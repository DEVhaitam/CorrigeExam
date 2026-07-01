"""
s04_import.py — Scenario 4: course-import workload (beginning-of-semester burst).

Rationale
---------
S01–S03 are dominated by GET operations and one PUT (grade). The import path
is entirely absent, yet it is the most resource-intensive operation in the
platform:

  - Server-side JSON deserialisation of the full course graph
  - Cascading INSERT transactions across 8+ tables
    (course, exam, template, question, zone, exam_sheet, student_response, …)
  - Hibernate flush for every entity → InnoDB row inserts + WAL flushes
  - canAccess() ownership check on every nested entity

This scenario targets the "start of semester" burst: multiple teachers racing
to set up their courses at the same time.

Two user classes
----------------
ImportUser  (weight=1, 20 % of spawned users)
  - on_start : auth + discover one course + export it WITHOUT student data
               → cache the JSON bytes in memory (one HTTP call, then reused)
  - Tasks:
      import_course        weight=1  POST /api/importCourseWithoutStudentData
      browse_my_courses    weight=4  GET  /api/courses
      browse_exams         weight=3  GET  /api/exams
      exam_detail          weight=2  GET  /api/exams/{id}
  - Think time: 10–30 s (import is expensive; teachers don't chain them
    back-to-back, but the think time is shorter than real life so we can
    generate measurable load in a 5-minute run)

BrowseUser (weight=4, 80 % of spawned users)
  Identical to s03 BrowseUser — provides a realistic background read load
  against which the import spikes are clearly visible in Grafana.

What to watch in Grafana
------------------------
  - process_cpu_usage                → JVM CPU during Hibernate flush storms
  - jvm_memory_used_bytes{area=heap} → object churn: every entity = Java object
  - hikaricp_connections_pending     → connection pool starvation (cascading
    inserts hold connections far longer than a simple SELECT)
  - mysql_global_status_innodb_row_lock_waits rate
                                     → lock contention between concurrent imports
  - mysql_global_status_com_insert rate
                                     → bulk insert throughput reaching MySQL
  - rate(http_server_requests_seconds_count{uri="/api/importCourseWithoutStudentData"})
                                     → actual import RPS

Infrastructure parameters most sensitive to this workload
---------------------------------------------------------
  - vCPU count  (Hibernate dirty checking + JSON serde is CPU-bound)
  - RAM         (InnoDB buffer pool + JVM heap for object graph)
  - Disk IOPS   (WAL flush on every committed INSERT batch)
  - DB pool size (long-running transactions → pool exhaustion)

Usage
-----
  # Web UI
  locust -f lab/scenarios/s04_import.py --host http://localhost:8082

  # Headless — 25 users (20 browse + 5 import), 2/s ramp, 5 minutes
  RUN_ID=$(date +%Y%m%d_%H%M%S)
  locust -f lab/scenarios/s04_import.py \\
         --host http://localhost:8082 \\
         --headless -u 25 -r 2 --run-time 5m \\
         --csv lab/results/${RUN_ID}_s04_import \\
         --html lab/results/${RUN_ID}_s04_import.html

  # Grid5000: same command, different host
  locust -f lab/scenarios/s04_import.py \\
         --host http://<vm-ip>:8082 \\
         --headless -u 25 -r 2 --run-time 5m \\
         --csv lab/results/vm1_${RUN_ID}_s04_import \\
         --html lab/results/vm1_${RUN_ID}_s04_import.html

Key metrics to compare across VMs
----------------------------------
  - POST /api/importCourseWithoutStudentData p50 / p95 latency
  - Overall req/s at fixed user count
  - hikaricp_connections_pending peak
  - process_cpu_usage peak during import burst
"""

import io
import os
import logging
import random
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


# ── BrowseUser (80 %) ─────────────────────────────────────────────────────────

class BrowseUser(HttpUser):
    """
    Background read load — identical to s03.
    Keeps the platform busy with realistic navigation traffic while
    ImportUsers fire heavy write bursts.
    """
    weight    = 4
    wait_time = between(1, 3)

    def on_start(self):
        self._token = _authenticate(self.client)
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        self._exam_ids:  list = []
        self._sheet_ids: list = []
        self._load_context()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            self._token = _authenticate(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _load_context(self):
        with self.client.get(
            "/api/exams?page=0&size=20",
            name="GET /api/exams (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._exam_ids = [e["id"] for e in r.json()]
            r.success()
        if self._exam_ids:
            eid = random.choice(self._exam_ids)
            with self.client.get(
                f"/api/exam-sheets?examId={eid}&page=0&size=200",
                name="GET /api/exam-sheets (setup)",
                catch_response=True,
            ) as r:
                if r.status_code == 200 and r.json():
                    self._sheet_ids = [s["id"] for s in r.json()]
                r.success()

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
            self._load_context()
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


# ── ImportUser (20 %) ─────────────────────────────────────────────────────────

class ImportUser(HttpUser):
    """
    Simulates a teacher setting up their course at the start of semester.

    on_start: discovers all courses, exports one (without student data) as JSON
    bytes. The export is done once and cached — each import_course task reuses
    the same bytes, which is realistic (same teacher importing the same course
    template into a new year/group).

    The import triggers a full cascading INSERT across all entity tables, which
    is the heaviest write path in the entire application.
    """
    weight    = 1
    wait_time = between(10, 30)

    def on_start(self):
        self._token = _authenticate(self.client)
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        self._course_ids: list = []
        self._exam_ids:   list = []
        # JSON bytes of the exported course, reused on every import call
        self._export_payloads: list[bytes] = []
        self._setup()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            self._token = _authenticate(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _setup(self):
        """Discover courses and pre-fetch export JSON for each."""
        with self.client.get(
            "/api/courses?page=0&size=20",
            name="GET /api/courses (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._course_ids = [c["id"] for c in r.json()]
            r.success()

        with self.client.get(
            "/api/exams?page=0&size=50",
            name="GET /api/exams (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._exam_ids = [e["id"] for e in r.json()]
            r.success()

        if not self._course_ids:
            logging.warning("ImportUser: no courses found — import tasks will be skipped")
            return

        # Export up to 2 courses so each ImportUser has variety in payload size.
        # Uses exportCourseWithoutStudentData — reliable path (no copy-paste bug).
        for cid in random.sample(self._course_ids, min(2, len(self._course_ids))):
            with self.client.get(
                f"/api/exportCourseWithoutStudentData/{cid}",
                name="GET /api/exportCourseWithoutStudentData",
                catch_response=True,
            ) as r:
                if r.status_code == 200 and r.content:
                    self._export_payloads.append(r.content)
                    logging.info(
                        "ImportUser: cached export for course %d (%d bytes)", cid, len(r.content)
                    )
                    r.success()
                else:
                    r.failure(f"export failed {r.status_code}")

    # ── tasks ─────────────────────────────────────────────────────────────────

    @task(1)
    def import_course(self):
        """
        THE heavy write path.

        POSTs cached JSON bytes as multipart/form-data to
        /api/importCourseWithoutStudentData. The server deserialises the full
        course graph and inserts all entities in a single Hibernate session:
          - course + courseGroup rows
          - exam, template, scan rows
          - question + zone rows
          - exam_sheet rows (without student data → no student_response rows)

        Each call creates a new independent course copy — no collision with
        existing data. DB grows by ~N rows per call (proportional to course
        size). In a 5-minute run at 10–30 s think time, each ImportUser fires
        ~10–30 imports — manageable for the experiment.
        """
        if not self._export_payloads:
            logging.warning("ImportUser: no export payload cached — skipping import")
            return

        payload = random.choice(self._export_payloads)

        # Strip the Authorization header for the multipart call — requests
        # sets Content-Type automatically when files= is used. We re-add auth
        # via the session header (already set in self.client.headers).
        with self.client.post(
            "/api/importCourseWithoutStudentData",
            files={"file": ("course.json", io.BytesIO(payload), "application/octet-stream")},
            name="POST /api/importCourseWithoutStudentData",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"import failed {r.status_code}: {r.text[:120]}")

    @task(4)
    def browse_my_courses(self):
        """Teacher checks their course list after importing."""
        with self.client.get(
            "/api/courses?page=0&size=20",
            name="GET /api/courses",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code == 200 else r.failure(f"status {r.status_code}")

    @task(3)
    def browse_exams(self):
        """Teacher navigates to the exam list of a course they just imported."""
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

    @task(2)
    def exam_detail(self):
        """Spot-check one exam after import."""
        if not self._exam_ids:
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
