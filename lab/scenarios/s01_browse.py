"""
s01_browse.py — Scenario 1: read-only browse workload.

Goal: verify the monitoring stack captures visible activity and establish
a baseline for what "idle" vs "loaded" looks like before adding write workloads.

What this scenario does (simulating a teacher reviewing exam results):
  1. Authenticate as admin (token cached per user, re-used across tasks)
  2. List all courses
  3. List all exams
  4. Fetch detail of a random exam
  5. List exam sheets for that exam
  6. List student responses for an exam sheet
  7. List questions for that exam
  8. Health ping (background noise, lower weight)

All operations are read-only GETs — no mutations, no file uploads.

Usage:
  # Web UI (http://localhost:8089) — start with 10 users, 2/s ramp
  locust -f lab/scenarios/s01_browse.py --host http://localhost:8082

  # Headless — 20 users, 2/s ramp, 5 minutes
  locust -f lab/scenarios/s01_browse.py \\
         --host http://localhost:8082 \\
         --headless -u 20 -r 2 --run-time 5m \\
         --csv results/s01_browse

What to watch in Grafana while this runs:
  - http_server_requests_seconds_count rate  (should rise with user count)
  - jvm_memory_used_bytes{area="heap"}       (baseline heap pressure)
  - jvm_gc_pause_seconds_sum rate            (GC activity)
  - hikaricp_connections_active              (DB pool usage)
  - mysql_global_status_queries rate         (MySQL QPS)
"""

import os
import random
import logging
from locust import HttpUser, task, between, events

BASE_URL = os.getenv("TARGET_HOST", "http://localhost:8082")
USERNAME  = os.getenv("LOCUST_USER", "admin")
PASSWORD  = os.getenv("LOCUST_PASS", "admin")


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


class BrowseUser(HttpUser):
    """
    Simulates a teacher browsing the platform: looking at courses, exams,
    student sheets, and responses. Pure read path.
    """

    wait_time = between(1, 3)   # realistic think time between page loads

    def on_start(self):
        self._token = _authenticate(self.client)
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})
        self._exam_ids = []
        self._sheet_ids = []
        # warm up local caches on first login
        self._refresh_exam_list()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            logging.warning("401 — re-authenticating")
            self._token = _authenticate(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _refresh_exam_list(self):
        with self.client.get(
            "/api/exams?page=0&size=50",
            name="GET /api/exams (list)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200:
                data = r.json()
                self._exam_ids = [e["id"] for e in data]
                r.success()
            else:
                r.failure(f"status {r.status_code}")

    # ── tasks ─────────────────────────────────────────────────────────────────

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
            name="GET /api/exams (list)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code == 200:
                data = r.json()
                if data:
                    self._exam_ids = [e["id"] for e in data]
                r.success()
            else:
                r.failure(f"status {r.status_code}")

    @task(4)
    def exam_detail(self):
        if not self._exam_ids:
            self._refresh_exam_list()
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
            name="GET /api/exam-sheets?examId=",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 204):
                data = r.json() if r.status_code == 200 else []
                if data:
                    # cache a few sheet IDs for the response task
                    self._sheet_ids = [s["id"] for s in data[:10]]
                r.success()
            else:
                r.failure(f"status {r.status_code}")

    @task(3)
    def list_questions(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/questions?examId={eid}&page=0&size=50",
            name="GET /api/questions?examId=",
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
            name="GET /api/student-responses?examSheetId=",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")

    @task(1)
    def list_final_results(self):
        """Lightweight read — lists final results for a random exam."""
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(
            f"/api/final-results?examId={eid}&page=0&size=50",
            name="GET /api/final-results?examId=",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            r.success() if r.status_code in (200, 204) else r.failure(f"status {r.status_code}")
