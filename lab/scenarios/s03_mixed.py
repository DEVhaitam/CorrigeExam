"""
s03_mixed.py — Scenario 3: mixed browse + grade (the Grid5000 reference workload).

This is the scenario to use for the provisioning experiment.
Run the EXACT same command against each VM and compare the results.

User mix (by weight):
  BrowseUser  weight=3  → 60% of spawned users  (teachers reviewing, students checking)
  GraderUser  weight=2  → 40% of spawned users  (teachers actively grading)

Why this split:
  On a live platform during an exam correction period, most users are passively
  reading (loading dashboards, checking progress, reviewing results) while a
  smaller but steady fraction is actively writing grades. 60/40 is a reasonable
  approximation of that state.

Usage:
  # Web UI — open http://localhost:8089 to control users live
  locust -f lab/scenarios/s03_mixed.py --host http://localhost:8082

  # Headless — save results to lab/results/
  RUN_ID=$(date +%Y%m%d_%H%M%S)
  locust -f lab/scenarios/s03_mixed.py \\
         --host http://localhost:8082 \\
         --headless -u 50 -r 5 --run-time 5m \\
         --csv lab/results/${RUN_ID} \\
         --html lab/results/${RUN_ID}.html

  # Grid5000: point at the VM under test (same command, different host)
  locust -f lab/scenarios/s03_mixed.py \\
         --host http://<vm-ip>:8082 \\
         --headless -u 50 -r 5 --run-time 5m \\
         --csv lab/results/vm1_heap512m_${RUN_ID} \\
         --html lab/results/vm1_heap512m_${RUN_ID}.html

Result files produced (prefix = value of --csv):
  <prefix>_stats.csv          — per-endpoint totals (req count, avg/p50/p95/p99, failures)
  <prefix>_stats_history.csv  — same metrics over time (one row per ~10s) — USE THIS for time plots
  <prefix>_failures.csv       — error details
  <prefix>.html               — standalone HTML report (shareable)

What to compare across VMs:
  - PUT /api/student-responses (grade) p95 latency  — write path sensitivity
  - GET /api/exams (list) p95 latency               — read path sensitivity
  - Overall req/s at the same user count             — throughput under fixed load
  - Failure rate                                      — resilience at saturation

What to watch in Grafana simultaneously:
  - hikaricp_connections_pending     → pool starvation (same app config, different hardware)
  - process_cpu_usage                → how much CPU the JVM uses on this VM
  - jvm_gc_pause_seconds_sum rate    → GC pressure (RAM-sensitive)
  - mysql_global_status_com_update rate → write throughput reaching MySQL
  - container_cpu_throttled_seconds  → CPU limit enforcement (if limits set)
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


# ── shared base ────────────────────────────────────────────────────────────────

class _BaseUser(HttpUser):
    """Common auth + context loading. Not used directly by locust (abstract)."""

    abstract = True

    def on_start(self):
        self._token = _authenticate(self.client)
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        self._exam_ids: list = []
        self._sheet_ids: list = []
        self._question_ids: list = []
        self._load_exam_context()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            logging.warning("401 — re-authenticating")
            self._token = _authenticate(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _load_exam_context(self):
        with self.client.get(
            "/api/exams?page=0&size=20",
            name="GET /api/exams (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._exam_ids = [e["id"] for e in r.json()]
            r.success()

        if not self._exam_ids:
            return

        eid = random.choice(self._exam_ids)

        with self.client.get(
            f"/api/exam-sheets?examId={eid}&page=0&size=200",
            name="GET /api/exam-sheets (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._sheet_ids = [s["id"] for s in r.json()]
            r.success()

        with self.client.get(
            f"/api/questions?examId={eid}&page=0&size=50",
            name="GET /api/questions (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._question_ids = [q["id"] for q in r.json()]
            r.success()


# ── BrowseUser (60%) ──────────────────────────────────────────────────────────

class BrowseUser(_BaseUser):
    """
    Read-only user: teacher reviewing results, checking progress, exploring the exam.
    Exercises the read path (MySQL SELECTs, Hibernate L2 cache, HTTP serialisation).
    60% of the user population.
    """

    weight = 3
    wait_time = between(1, 3)

    @task(5)
    def list_courses(self):
        with self.client.get(
            "/api/courses?page=0&size=20",
            name="GET /api/courses",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code == 200 else r.failure(f"status {r.status_code}")

    @task(4)
    def list_exams(self):
        with self.client.get(
            "/api/exams?page=0&size=50",
            name="GET /api/exams",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200 and r.json():
                self._exam_ids = [e["id"] for e in r.json()]
            r.success() if r.status_code == 200 else r.failure(f"status {r.status_code}")

    @task(4)
    def exam_detail(self):
        if not self._exam_ids:
            self._load_exam_context()
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/exams/{eid}",
            name="GET /api/exams/{id}",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 404) else r.failure(f"status {r.status_code}")

    @task(3)
    def list_exam_sheets(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/exam-sheets?examId={eid}&page=0&size=50",
            name="GET /api/exam-sheets",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200 and r.json():
                self._sheet_ids = [s["id"] for s in r.json()]
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(3)
    def list_questions(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/questions?examId={eid}&page=0&size=50",
            name="GET /api/questions",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(2)
    def list_students(self):
        with self.client.get(
            "/api/students?page=0&size=50",
            name="GET /api/students",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(2)
    def list_student_responses(self):
        if not self._sheet_ids:
            return
        sid = random.choice(self._sheet_ids)
        with self.client.get(
            f"/api/student-responses?examSheetId={sid}&page=0&size=50",
            name="GET /api/student-responses",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(1)
    def list_final_results(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/final-results?examId={eid}&page=0&size=50",
            name="GET /api/final-results",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")


# ── GraderUser (40%) ──────────────────────────────────────────────────────────

class GraderUser(_BaseUser):
    """
    Write-heavy user: teacher actively grading student responses.
    Exercises MySQL write path (InnoDB row updates, WAL flushes, lock contention).
    40% of the user population.
    """

    weight = 2
    wait_time = between(1, 4)   # graders think longer between actions

    def on_start(self):
        super().on_start()
        # Cache: sheet_id → list of response dicts
        self._responses: dict = {}
        # Pre-load a few sheets so grading can start immediately
        for sid in random.sample(self._sheet_ids, min(3, len(self._sheet_ids))):
            self._fetch_sheet_responses(sid)

    def _fetch_sheet_responses(self, sheet_id: int) -> list:
        with self.client.get(
            f"/api/student-responses?examSheetId={sheet_id}&page=0&size=50",
            name="GET /api/student-responses (load sheet)",
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

    @task(4)
    def grade_response(self):
        """Save a grade — the write hot path."""
        candidates = self._all_cached_responses()
        if not candidates:
            if self._sheet_ids:
                self._fetch_sheet_responses(random.choice(self._sheet_ids))
            return

        resp = random.choice(candidates)
        new_note = round(random.uniform(0, 20), 1)

        payload = {
            "id":             resp["id"],
            "note":           new_note,
            "questionId":     resp.get("questionId"),
            "sheetId":        resp.get("sheetId"),
            "star":           resp.get("star", False),
            "worststar":      resp.get("worststar", False),
            "gradedcomments": resp.get("gradedcomments", []),
            "textcomments":   resp.get("textcomments", []),
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
                resp["note"] = new_note
                r.success()
            else:
                r.failure(f"grade failed {r.status_code}: {r.text[:100]}")

    @task(4)
    def view_sheet(self):
        """Load a student sheet before grading it."""
        if not self._sheet_ids:
            return
        self._fetch_sheet_responses(random.choice(self._sheet_ids))

    @task(2)
    def check_progress(self):
        """Check how many sheets have been graded."""
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/exam-sheets?examId={eid}&page=0&size=200",
            name="GET /api/exam-sheets (progress)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(1)
    def switch_question(self):
        """Pick a different question to grade."""
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/questions?examId={eid}&page=0&size=50",
            name="GET /api/questions (switch)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")
