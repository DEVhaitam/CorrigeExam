"""
import_stress.py — CorrectExam stress scenarios focused on the importCourse
endpoint and post-import load patterns.

Three scenarios (select via SCENARIO env var):

  A  light_import   One user at a time imports 357.json (85 MB), then browses.
                    Baseline: what does a single heavy import look like in JVM?

  B  import_storm   N concurrent users each repeatedly POST 357.json to
                    /api/importCourse. Stresses heap (JSON parsing), MinIO writes,
                    and MySQL bulk inserts simultaneously.

  C  realistic      After a warm-up import, 70% browse / 20% grade / 10% import.
                    Mirrors a real class session where a professor imports a new
                    batch while students are being graded.

Running (after seed.py has populated at least one course):

  # Scenario A — single-user import baseline, 5 min
  SCENARIO=A locust -f workload/locust/import_stress.py \\
    --host http://localhost:8082 --headless -u 1 -r 1 --run-time 5m

  # Scenario B — import storm, 5 concurrent users, 10 min
  SCENARIO=B locust -f workload/locust/import_stress.py \\
    --host http://localhost:8082 --headless -u 5 -r 1 --run-time 10m

  # Scenario C — realistic mixed, 20 users, 15 min
  SCENARIO=C locust -f workload/locust/import_stress.py \\
    --host http://localhost:8082 --headless -u 20 -r 2 --run-time 15m

  # UI mode (pick scenario interactively)
  locust -f workload/locust/import_stress.py --host http://localhost:8082
"""

import os
import random
import logging
from pathlib import Path

from locust import HttpUser, task, between, events

SCENARIO = os.getenv("SCENARIO", "C").upper()

_TESTDATA = Path(__file__).parent.parent.parent / "testData"
_SMALL  = _TESTDATA / "357.json"   # ~85 MB  — use this one (136.json has corrupt data)
# 136.json is EXCLUDED: 415/416 comments have zonegeneratedid="255_null_*",
# causing Long.parseLong("null") → NumberFormatException → silent 500 in ImportExportService.
# 265.json (~1 GB) is reserved for explicit heap-ceiling experiments only.

# Load once at startup — avoids repeated disk I/O during the test
def _load(path: Path) -> bytes:
    if path.exists():
        data = path.read_bytes()
        logging.info("Loaded %s (%.1f MB)", path.name, len(data) / 1_000_000)
        return data
    logging.warning("testData file not found: %s — import tasks will be skipped", path)
    return b""

_SMALL_BYTES = _load(_SMALL)


# ── auth helper ───────────────────────────────────────────────────────────────

def _get_token(client, user="admin", pwd="admin") -> str:
    resp = client.post(
        "/api/authenticate",
        json={"username": user, "password": pwd, "rememberMe": False},
        name="/api/authenticate",
    )
    resp.raise_for_status()
    return resp.json()["id_token"]


# ── Scenario A & B: Import user ────────────────────────────────────────────────

class ImportUser(HttpUser):
    """
    Repeatedly POSTs a course bundle to /api/importCourse.
    Scenario A: 1 user (baseline), Scenario B: N users (storm).
    The import endpoint parses the full JSON, writes to MySQL, and uploads PDFs
    to MinIO — all three bottlenecks at once.
    """
    weight = 1 if SCENARIO in ("A", "B") else 1  # always included; scenario C caps via weight
    wait_time = between(1, 3)

    _token: str = ""

    def on_start(self):
        self._token = _get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            self._token = _get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    @task
    def import_small(self):
        """POST 357.json (~85 MB) — the only valid import file for stress testing."""
        if not _SMALL_BYTES:
            return
        with self.client.post(
            "/api/importCourse",
            files={"file": ("357.json", _SMALL_BYTES, "application/json")},
            name="POST /api/importCourse (357.json ~85MB)",
            catch_response=True,
            timeout=300,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 201):
                r.success()
            else:
                r.failure(f"importCourse failed {r.status_code}: {r.text[:200]}")


# ── Browse user (used in Scenario C) ──────────────────────────────────────────

class BrowseUser(HttpUser):
    """
    Light read-only pattern — mimics a professor browsing courses and exams
    while an import is in progress in the background.
    """
    weight = 7 if SCENARIO == "C" else 0
    wait_time = between(1, 3)

    _token: str = ""
    _exam_ids: list = []
    _course_ids: list = []

    def on_start(self):
        self._token = _get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            self._token = _get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    @task(5)
    def list_courses(self):
        with self.client.get("/api/courses?page=0&size=20", name="GET /api/courses",
                             catch_response=True) as r:
            if self._reauth(r):
                return
            if r.status_code == 200:
                data = r.json()
                if data:
                    self._course_ids = [c["id"] for c in data[:10]]
                r.success()
            else:
                r.failure(f"list_courses {r.status_code}")

    @task(4)
    def list_exams(self):
        with self.client.get("/api/exams?page=0&size=20", name="GET /api/exams",
                             catch_response=True) as r:
            if self._reauth(r):
                return
            if r.status_code == 200:
                data = r.json()
                if data:
                    self._exam_ids = [e["id"] for e in data[:10]]
                r.success()
            else:
                r.failure(f"list_exams {r.status_code}")

    @task(2)
    def exam_detail(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(f"/api/exams/{eid}", name="GET /api/exams/{id}",
                             catch_response=True) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"exam_detail {r.status_code}")

    @task(1)
    def health_ping(self):
        with self.client.get("/management/health", name="GET /management/health",
                             catch_response=True) as r:
            if r.status_code == 200:
                r.success()
            else:
                r.failure(f"health {r.status_code}")


# ── Grade user (used in Scenario C) ───────────────────────────────────────────

class GradeUser(HttpUser):
    """
    DB write-heavy grading path — runs concurrently with imports in Scenario C
    to stress MySQL lock contention.
    """
    weight = 2 if SCENARIO == "C" else 0
    wait_time = between(0.5, 2)

    _token: str = ""
    _exam_id: int = None
    _sheet_ids: list = []
    _question_ids: list = []

    def on_start(self):
        self._token = _get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"
        self._fetch_context()

    def _reauth(self, resp) -> bool:
        if resp.status_code == 401:
            self._token = _get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _fetch_context(self):
        with self.client.get("/api/exams?page=0&size=5", name="GET /api/exams (setup)",
                             catch_response=True) as r:
            if r.status_code == 200 and r.json():
                self._exam_id = r.json()[0]["id"]
            r.success()
        if not self._exam_id:
            return
        with self.client.get(
            f"/api/exam-sheets?examId={self._exam_id}&page=0&size=50",
            name="GET /api/exam-sheets (setup)", catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._sheet_ids = [s["id"] for s in r.json()[:20]]
            r.success()
        with self.client.get(
            f"/api/questions?examId={self._exam_id}&page=0&size=50",
            name="GET /api/questions (setup)", catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._question_ids = [q["id"] for q in r.json()[:20]]
            r.success()

    @task(8)
    def save_grade(self):
        if not self._sheet_ids or not self._question_ids:
            self._fetch_context()
            return
        with self.client.post(
            "/api/student-responses",
            json={
                "note": round(random.uniform(0, 20), 1),
                "examSheetId": random.choice(self._sheet_ids),
                "questionId": random.choice(self._question_ids),
            },
            name="POST /api/student-responses (grade)",
            catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 201):
                r.success()
            else:
                r.failure(f"save_grade {r.status_code}: {r.text[:200]}")

    @task(2)
    def fetch_sheets(self):
        if not self._exam_id:
            return
        with self.client.get(
            f"/api/exam-sheets?examId={self._exam_id}&page=0&size=20",
            name="GET /api/exam-sheets", catch_response=True,
        ) as r:
            if self._reauth(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"fetch_sheets {r.status_code}")


# ── metadata ───────────────────────────────────────────────────────────────────

@events.test_start.add_listener
def _on_start(environment, **kwargs):
    import sys
    from pathlib import Path as P
    csv_prefix = None
    for i, arg in enumerate(sys.argv):
        if arg == "--csv" and i + 1 < len(sys.argv):
            csv_prefix = sys.argv[i + 1]
            break
    if not csv_prefix:
        return
    results_dir = P(csv_prefix).parent
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "metadata.yaml").write_text(
        f"scenario: import_stress_{SCENARIO}\n"
        f"import_file: {_SMALL} ({len(_SMALL_BYTES) // 1_000_000} MB)\n"
        f"note: 136.json excluded (corrupt zonegeneratedid), 265.json excluded (1 GB heap test only)\n"
    )
