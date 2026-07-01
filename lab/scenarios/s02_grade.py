"""
s02_grade.py — Scenario 2: realistic grading workload (read + write mix).

What a teacher actually does in CorrectExam:
  1. Opens an exam, picks a question to grade
  2. For each student: loads the sheet responses (GET), reads the answer, saves a grade (PUT)
  3. Occasionally checks overall progress (GET exam-sheets)

This scenario exercises the DB write path. Compared to s01_browse (pure reads),
this adds:
  - PUT /api/student-responses   → InnoDB row update + WAL flush (disk I/O sensitive)
  - canAccess() on every write   → additional DB read per PUT (CPU + connection pool)
  - Concurrent writes to the same table → InnoDB row-level lock contention

Infrastructure parameters most sensitive to this workload:
  - Disk throughput / IOPS      (InnoDB WAL on every committed write)
  - vCPU count / speed          (Hibernate dirty checking, canAccess DB calls, JSON serde)
  - RAM                         (InnoDB buffer pool — keeps hot rows in memory)
  - DB connection pool size      (every write uses a connection + a canAccess read)

No hardcoded IDs: all exam/sheet/response IDs are discovered at runtime from the API.

Usage:
  # Web UI
  locust -f lab/scenarios/s02_grade.py --host http://localhost:8082

  # Headless — 50 concurrent graders, 5/s ramp, 5 minutes
  locust -f lab/scenarios/s02_grade.py \\
         --host http://localhost:8082 \\
         --headless -u 50 -r 5 --run-time 5m \\
         --csv results/s02_grade

Recommended starting concurrency: 20–50 users.
Anything above ~200 will overwhelm the 8-connection Hikari pool and you will
see the same bimodal latency pattern as s01 at high user counts.
That saturation IS the signal — do not try to "fix" it by raising the pool size,
because the pool size is an IaC parameter we will vary in later experiments.

What to watch in Grafana:
  - PUT /api/student-responses rate + p95 latency  (the write hot path)
  - hikaricp_connections_pending                   (pool starvation indicator)
  - mysql_global_status_innodb_row_lock_waits rate (lock contention)
  - mysql_global_status_com_update rate            (InnoDB write rate)
  - rate(mysql_global_status_innodb_data_writes)   (disk write activity)
  - jvm_memory_used_bytes{area="heap"}             (object churn from Hibernate)
"""

import os
import random
import logging
from locust import HttpUser, task, between

USERNAME = os.getenv("LOCUST_USER", "admin")
PASSWORD = os.getenv("LOCUST_PASS", "admin")


def _authenticate(client, username=USERNAME, password=PASSWORD) -> str:
    resp = client.post(
        "/api/authenticate",
        json={"username": username, "password": password, "rememberMe": False},
        name="POST /api/authenticate",
    )
    if resp.status_code != 200:
        logging.error("Auth failed %s: %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()
    return resp.json()["id_token"]


class GraderUser(HttpUser):
    """
    Simulates a teacher grading student responses:
    load sheet → read responses → save grade → repeat.

    Task weights reflect realistic teacher behaviour:
      - for every grade saved, roughly one sheet was loaded first
      - progress checks and question navigation happen less often
    """

    wait_time = between(1, 4)   # teacher think time between actions

    def on_start(self):
        self._token = _authenticate(self.client)
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        self._exam_id = None
        self._sheet_ids = []
        self._question_ids = []
        # Cache: sheet_id → list of response dicts (id, note, questionId, sheetId, …)
        self._responses: dict[int, list] = {}
        self._setup_context()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            logging.warning("401 — re-authenticating")
            self._token = _authenticate(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _setup_context(self):
        """Discover exam → sheets → questions → pre-load a few sheets' responses."""
        # Pick a random exam that has sheets
        with self.client.get(
            "/api/exams?page=0&size=20",
            name="GET /api/exams (setup)",
            catch_response=True,
        ) as r:
            if r.status_code != 200 or not r.json():
                r.success()
                return
            self._exam_id = random.choice(r.json())["id"]
            r.success()

        if not self._exam_id:
            return

        # Get all sheets for this exam
        with self.client.get(
            f"/api/exam-sheets?examId={self._exam_id}&page=0&size=200",
            name="GET /api/exam-sheets (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._sheet_ids = [s["id"] for s in r.json()]
            r.success()

        # Get all questions for this exam
        with self.client.get(
            f"/api/questions?examId={self._exam_id}&page=0&size=50",
            name="GET /api/questions (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._question_ids = [q["id"] for q in r.json()]
            r.success()

        # Pre-load responses for 3 random sheets so grade_response can start immediately
        for sid in random.sample(self._sheet_ids, min(3, len(self._sheet_ids))):
            self._fetch_sheet_responses(sid, name_suffix="(setup)")

    def _fetch_sheet_responses(self, sheet_id: int, name_suffix: str = "") -> list:
        """GET responses for one sheet, cache them, return the list."""
        with self.client.get(
            f"/api/student-responses?examSheetId={sheet_id}&page=0&size=50",
            name=f"GET /api/student-responses {name_suffix}".strip(),
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return []
            if r.status_code == 200 and r.json():
                self._responses[sheet_id] = r.json()
                r.success()
                return r.json()
            r.success()
            return []

    def _all_cached_responses(self) -> list:
        return [resp for responses in self._responses.values() for resp in responses]

    # ── tasks ──────────────────────────────────────────────────────────────────

    @task(4)
    def grade_response(self):
        """
        THE write hot path.
        Teacher has read a student response and now saves a grade.
        Triggers: InnoDB row update + WAL flush + canAccess DB reads.
        """
        candidates = self._all_cached_responses()
        if not candidates:
            # No responses loaded yet — load a sheet first
            if self._sheet_ids:
                self._fetch_sheet_responses(
                    random.choice(self._sheet_ids), name_suffix="(on-demand)"
                )
            return

        resp = random.choice(candidates)
        new_note = round(random.uniform(0, 20), 1)

        payload = {
            "id":            resp["id"],
            "note":          new_note,
            "questionId":    resp.get("questionId"),
            "sheetId":       resp.get("sheetId"),
            "star":          resp.get("star", False),
            "worststar":     resp.get("worststar", False),
            "gradedcomments": resp.get("gradedcomments", []),
            "textcomments":  resp.get("textcomments", []),
        }

        with self.client.put(
            "/api/student-responses",
            json=payload,
            name="PUT /api/student-responses (grade)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200:
                resp["note"] = new_note   # keep local cache consistent
                r.success()
            else:
                r.failure(f"grade failed {r.status_code}: {r.text[:100]}")

    @task(4)
    def view_sheet(self):
        """
        Teacher navigates to a student's sheet to see all their responses.
        Precedes a grade in the real flow: read first, then grade.
        """
        if not self._sheet_ids:
            return
        self._fetch_sheet_responses(
            random.choice(self._sheet_ids), name_suffix="(view)"
        )

    @task(2)
    def check_progress(self):
        """
        Teacher checks overall grading progress — which sheets still have
        ungraded responses. Also loads the exam sheet list view.
        """
        if not self._exam_id:
            return
        with self.client.get(
            f"/api/exam-sheets?examId={self._exam_id}&page=0&size=200",
            name="GET /api/exam-sheets (progress)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(1)
    def switch_question(self):
        """
        Teacher switches to a different question to grade.
        Triggers a question list fetch — relatively rare.
        """
        if not self._exam_id:
            return
        with self.client.get(
            f"/api/questions?examId={self._exam_id}&page=0&size=50",
            name="GET /api/questions (switch)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200 and r.json():
                self._question_ids = [q["id"] for q in r.json()]
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")
