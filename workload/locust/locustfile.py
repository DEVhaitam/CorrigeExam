"""
locustfile.py — CorrectExam load workloads W1–W4.

Profiles (select via --tags or WORKLOAD env var):
  W1  browse   light read-only: courses / exams / students / health
  W2  upload   heap+IO dominant: create exam + PDF upload via multipart
  W3  grade    DB write-heavy: fetch sheets + save grades
  W4  mixed    60/20/20 mix of W1/W2/W3 with diurnal LoadTestShape

Running:
  # Headless, W2 medium intensity
  WORKLOAD=upload SCENARIO=S1-heap-256m INTENSITY=medium EXPERIMENT_ID=<uuid> \\
    locust -f workload/locust/locustfile.py \\
           --host http://localhost:8082 \\
           --headless -u 10 -r 1 --run-time 6m30s \\
           --csv results/<run_id>/locust

  # W4 diurnal (LoadTestShape controls user count; -u is the burst ceiling)
  WORKLOAD=mixed SCENARIO=S7-cpu-cap-half INTENSITY=medium \\
    locust -f workload/locust/locustfile.py \\
           --host http://localhost:8082 \\
           --headless -u 60 -r 5 --run-time 26m \\
           --csv results/<run_id>/locust

Mandatory env vars (every run must set these — missing labels = broken joins):
  EXPERIMENT_ID   UUID for this run (set by run.py)
  SCENARIO        e.g. S1-heap-256m
  WORKLOAD        W1-browse | W2-upload | W3-grade | W4-mixed
  INTENSITY       low | medium | high

Adjustable endpoint constants (override via env if CorrectExam paths differ):
  SCAN_CREATE_PATH         default /api/scans (JSON, creates Scan entity → returns id)
  SCAN_UPLOAD_PATH         default /api/uploadScan (POST /{id} multipart field "file")
  GRADE_PATH_PREFIX        default /api/student-responses
  EXAM_SHEET_LIST_PATH     default /api/exam-sheets
  QUESTION_LIST_PATH       default /api/questions
"""

import os
import sys
import uuid
import random
import logging
from pathlib import Path
from locust import HttpUser, task, tag, between, events
from locust.shape import LoadTestShape

# ── labels ────────────────────────────────────────────────────────────────────
EXPERIMENT_ID = os.getenv("EXPERIMENT_ID", str(uuid.uuid4()))
SCENARIO      = os.getenv("SCENARIO", "unknown")
WORKLOAD      = os.getenv("WORKLOAD", "W1-browse")
INTENSITY     = os.getenv("INTENSITY", "unknown")

# ── adjustable endpoint constants ─────────────────────────────────────────────
SCAN_CREATE_PATH       = os.getenv("SCAN_CREATE_PATH", "/api/scans")
SCAN_UPLOAD_PATH       = os.getenv("SCAN_UPLOAD_PATH", "/api/uploadScan")
GRADE_PATH_PREFIX      = os.getenv("GRADE_PATH_PREFIX", "/api/student-responses")
EXAM_SHEET_LIST_PATH   = os.getenv("EXAM_SHEET_LIST_PATH", "/api/exam-sheets")
QUESTION_LIST_PATH     = os.getenv("QUESTION_LIST_PATH", "/api/questions")

# ── synthetic PDF pool ────────────────────────────────────────────────────────
_PDF_DIR = Path(__file__).parent.parent / "data" / "synthetic-exams"
_PDF_SMALL  = _PDF_DIR / "exam-200k.pdf"
_PDF_MEDIUM = _PDF_DIR / "exam-2m.pdf"
_PDF_LARGE  = _PDF_DIR / "exam-10m.pdf"

def _load_pdf_pool():
    """Return list of (path, size_label) tuples with skewed distribution.
    Falls back gracefully if generate_pdfs.py hasn't been run yet."""
    pool = []
    for path, label, weight in [
        (_PDF_SMALL,  "200k", 70),
        (_PDF_MEDIUM, "2m",   25),
        (_PDF_LARGE,  "10m",   5),
    ]:
        if path.exists():
            pool.extend([(path, label)] * weight)
    if not pool:
        logging.warning(
            "No synthetic PDFs found in %s. "
            "Run workload/data/generate_pdfs.py first.",
            _PDF_DIR,
        )
    return pool

_PDF_POOL = _load_pdf_pool()


def _pick_pdf():
    """Return bytes of a randomly selected synthetic PDF."""
    if not _PDF_POOL:
        return b"%PDF-1.4 1 0 obj<</Type /Catalog>> endobj\n%%EOF\n"
    path, _ = random.choice(_PDF_POOL)
    return path.read_bytes()


# ── W1 — browse (light read-only) ─────────────────────────────────────────────
class BrowseUser(HttpUser):
    """
    W1: light read workload. Stresses JVM CPU + MySQL read path.
    Control workload for L1 heap and L4 CPU scenarios.
    Intensity axis: users ∈ {5, 20, 50}
    """

    weight = 3 if WORKLOAD == "W4-mixed" else (1 if WORKLOAD == "W1-browse" else 0)
    wait_time = between(1, 3)

    _token: str = ""
    _exam_ids: list = []
    _course_ids: list = []

    def on_start(self):
        from workload.locust.auth import get_token
        self._token = get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"

    def _reauth_if_needed(self, resp) -> bool:
        if resp.status_code == 401:
            from workload.locust.auth import get_token
            self._token = get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    @task(5)
    @tag("browse", "mixed")
    def list_courses(self):
        with self.client.get("/api/courses?page=0&size=20", name="GET /api/courses",
                             catch_response=True) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code == 200:
                data = r.json()
                if data:
                    self._course_ids = [c["id"] for c in data[:10]]
                r.success()
            else:
                r.failure(f"Unexpected status {r.status_code}")

    @task(4)
    @tag("browse", "mixed")
    def list_exams(self):
        with self.client.get("/api/exams?page=0&size=20", name="GET /api/exams",
                             catch_response=True) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code == 200:
                data = r.json()
                if data:
                    self._exam_ids = [e["id"] for e in data[:10]]
                r.success()
            else:
                r.failure(f"Unexpected status {r.status_code}")

    @task(3)
    @tag("browse", "mixed")
    def list_students(self):
        with self.client.get("/api/students?page=0&size=50", name="GET /api/students",
                             catch_response=True) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"Unexpected status {r.status_code}")

    @task(2)
    @tag("browse", "mixed")
    def exam_detail(self):
        if not self._exam_ids:
            return
        eid = random.choice(self._exam_ids)
        with self.client.get(f"/api/exams/{eid}", name="GET /api/exams/{id}",
                             catch_response=True) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"Unexpected status {r.status_code}")

    @task(1)
    @tag("browse", "mixed")
    def health_ping(self):
        with self.client.get("/management/health", name="GET /management/health",
                             catch_response=True) as r:
            if r.status_code == 200:
                r.success()
            else:
                r.failure(f"Health check failed {r.status_code}")


# ── W2 — upload (heap + I/O dominant) ─────────────────────────────────────────
class UploadUser(HttpUser):
    """
    W2: PDF upload workload. Headline scenario for L1 -Xmx perturbations.
    Stresses JVM heap (PDFs buffered) and MinIO write bandwidth.
    Intensity axis: users ∈ {3, 10, 25}
    """

    weight = 1 if WORKLOAD in ("W2-upload", "W4-mixed") else 0
    wait_time = between(2, 5)

    _token: str = ""
    _scan_id: int = None
    _exam_id: int = None

    def on_start(self):
        from workload.locust.auth import get_token
        self._token = get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"
        self._setup_upload_context()

    def _reauth_if_needed(self, resp) -> bool:
        if resp.status_code == 401:
            from workload.locust.auth import get_token
            self._token = get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _setup_upload_context(self):
        """Build the ownership chain required by canAccess on uploadScan.

        CourseResource.createCourse auto-adds the creator to course.profs.
        Scan.canAccess checks: exam.scanfile == scan AND course.profs contains user.
        So we need: course (user as prof) → exam (courseId + scanfileId) → scan.
        The scan_id is reused across upload_pdf calls (overwrite is fine for load).
        """
        uid = uuid.uuid4().hex[:8]

        # 1. Course — creator is auto-added as prof by CourseResource.createCourse
        with self.client.post(
            "/api/courses",
            json={"name": f"locust-course-{uid}"},
            name="POST /api/courses (setup)",
            catch_response=True,
        ) as r:
            if r.status_code not in (200, 201):
                r.failure(f"Course create failed {r.status_code}: {r.text[:200]}")
                return
            course_id = r.json().get("id")
            r.success()

        # 2. Scan entity — placeholder; content uploaded via uploadScan
        with self.client.post(
            SCAN_CREATE_PATH,
            json={"name": f"locust-scan-{uid}"},
            name=f"POST {SCAN_CREATE_PATH} (setup)",
            catch_response=True,
        ) as r:
            if r.status_code not in (200, 201):
                r.failure(f"Scan create failed {r.status_code}: {r.text[:200]}")
                return
            self._scan_id = r.json().get("id")
            r.success()

        # 3. Exam — links course (prof chain) and scanfile (ownership check)
        with self.client.post(
            "/api/exams",
            json={
                "name":       f"locust-exam-{uid}",
                "courseId":   course_id,
                "scanfileId": self._scan_id,
            },
            name="POST /api/exams (setup)",
            catch_response=True,
        ) as r:
            if r.status_code not in (200, 201):
                r.failure(f"Exam create failed {r.status_code}: {r.text[:200]}")
                return
            self._exam_id = r.json().get("id")
            r.success()

    @task(6)
    @tag("upload", "mixed")
    def upload_pdf(self):
        if not self._scan_id:
            self._setup_upload_context()
            return
        # POST /api/uploadScan/{scanId} — multipart field "file"
        # canAccess passes because scan is exam.scanfile and user is course.prof
        pdf_bytes = _pick_pdf()
        headers = {"Authorization": f"Bearer {self._token}"}
        with self.client.post(
            f"{SCAN_UPLOAD_PATH}/{self._scan_id}",
            files={"file": ("scan.pdf", pdf_bytes, "application/pdf")},
            headers=headers,
            name=f"POST {SCAN_UPLOAD_PATH}/{{scanId}}",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 201, 202, 204):
                r.success()
            else:
                r.failure(f"Upload failed {r.status_code}: {r.text[:200]}")

    @task(1)
    @tag("upload", "mixed")
    def verify_upload(self):
        if not self._exam_id:
            return
        with self.client.get(
            f"{EXAM_SHEET_LIST_PATH}?examId={self._exam_id}&page=0&size=5",
            name=f"GET {EXAM_SHEET_LIST_PATH} (verify)",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"Verify failed {r.status_code}")


# ── W3 — grade (DB write-heavy) ───────────────────────────────────────────────
class GradeUser(HttpUser):
    """
    W3: grading workload. Headline for L3 MySQL scenarios (S4, S5, S6).
    Stresses MySQL write path, lock contention, result aggregation.
    Intensity axis: users ∈ {5, 20, 50}
    """

    weight = 1 if WORKLOAD in ("W3-grade", "W4-mixed") else 0
    wait_time = between(0.5, 2)

    _token: str = ""
    _exam_id: int = None
    _sheet_ids: list = []
    _question_ids: list = []

    def on_start(self):
        from workload.locust.auth import get_token
        self._token = get_token(self.client)
        self.client.headers["Authorization"] = f"Bearer {self._token}"
        self._fetch_exam_context()

    def _reauth_if_needed(self, resp) -> bool:
        if resp.status_code == 401:
            from workload.locust.auth import get_token
            self._token = get_token(self.client)
            self.client.headers["Authorization"] = f"Bearer {self._token}"
            return True
        return False

    def _fetch_exam_context(self):
        """Grab a real exam id + its sheets and questions to grade."""
        with self.client.get("/api/exams?page=0&size=5", name="GET /api/exams (setup)",
                             catch_response=True) as r:
            if r.status_code == 200 and r.json():
                self._exam_id = r.json()[0]["id"]
                r.success()
            else:
                r.success()  # not a failure — just no data yet
                return

        if not self._exam_id:
            return

        with self.client.get(
            f"{EXAM_SHEET_LIST_PATH}?examId={self._exam_id}&page=0&size=50",
            name=f"GET {EXAM_SHEET_LIST_PATH} (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._sheet_ids = [s["id"] for s in r.json()[:20]]
            r.success()

        with self.client.get(
            f"{QUESTION_LIST_PATH}?examId={self._exam_id}&page=0&size=50",
            name=f"GET {QUESTION_LIST_PATH} (setup)",
            catch_response=True,
        ) as r:
            if r.status_code == 200 and r.json():
                self._question_ids = [q["id"] for q in r.json()[:20]]
            r.success()

    @task(2)
    @tag("grade", "mixed")
    def fetch_sheets(self):
        if not self._exam_id:
            self._fetch_exam_context()
            return
        with self.client.get(
            f"{EXAM_SHEET_LIST_PATH}?examId={self._exam_id}&page=0&size=20",
            name=f"GET {EXAM_SHEET_LIST_PATH}",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"fetch_sheets failed {r.status_code}")

    @task(1)
    @tag("grade", "mixed")
    def fetch_questions(self):
        if not self._exam_id:
            return
        with self.client.get(
            f"{QUESTION_LIST_PATH}?examId={self._exam_id}",
            name=f"GET {QUESTION_LIST_PATH}",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 204):
                r.success()
            else:
                r.failure(f"fetch_questions failed {r.status_code}")

    @task(8)
    @tag("grade", "mixed")
    def save_grade(self):
        """THE hot path: saves a student grade. Adjust GRADE_PATH_PREFIX if
        CorrectExam uses a different entity (e.g. /api/graded-comments)."""
        if not self._sheet_ids or not self._question_ids:
            self._fetch_exam_context()
            return
        sheet_id    = random.choice(self._sheet_ids)
        question_id = random.choice(self._question_ids)
        payload = {
            "note": round(random.uniform(0, 20), 1),
            "examSheetId": sheet_id,
            "questionId": question_id,
        }
        with self.client.post(
            GRADE_PATH_PREFIX,
            json=payload,
            name=f"POST {GRADE_PATH_PREFIX} (save grade)",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 201):
                r.success()
            else:
                r.failure(f"save_grade failed {r.status_code}: {r.text[:200]}")

    @task(1)
    @tag("grade", "mixed")
    def fetch_results(self):
        if not self._exam_id:
            return
        with self.client.get(
            f"/api/exams/{self._exam_id}/results",
            name="GET /api/exams/{id}/results (aggregation)",
            catch_response=True,
        ) as r:
            if self._reauth_if_needed(r):
                return
            if r.status_code in (200, 204, 404):
                r.success()
            else:
                r.failure(f"fetch_results failed {r.status_code}")


# ── W4 — mixed (diurnal LoadTestShape) ────────────────────────────────────────
# User weights (60/20/20) are set on BrowseUser(3), UploadUser(1), GradeUser(1)
# above via WORKLOAD conditional. The shape controls the total user count.

_DIURNAL_TARGET = {
    "low":    10,
    "medium": 30,
    "high":   60,
}.get(INTENSITY, 30)


if WORKLOAD == "W4-mixed":
    class DiurnalShape(LoadTestShape):
        """
        W4 diurnal pattern. Only registered when WORKLOAD == W4-mixed so that
        W1/W2/W3 runs use plain --run-time / --users without shape interference.

        Phase        Duration  Users
        ramp-up      5 min     0 → target
        plateau      10 min    target
        burst        1 min     target × 2
        settle       4 min     target
        ramp-down    5 min     target → 0
        Total:       25 min
        """

        stages = [
            {"duration":  5 * 60, "users": _DIURNAL_TARGET,     "spawn_rate": max(1, _DIURNAL_TARGET // 30)},
            {"duration": 15 * 60, "users": _DIURNAL_TARGET,     "spawn_rate": 1},
            {"duration": 16 * 60, "users": _DIURNAL_TARGET * 2, "spawn_rate": max(1, _DIURNAL_TARGET // 10)},
            {"duration": 20 * 60, "users": _DIURNAL_TARGET,     "spawn_rate": max(1, _DIURNAL_TARGET // 10)},
            {"duration": 25 * 60, "users": 0,                   "spawn_rate": max(1, _DIURNAL_TARGET // 30)},
        ]

        def tick(self):
            run_time = self.get_run_time()
            for stage in self.stages:
                if run_time < stage["duration"]:
                    return stage["users"], stage["spawn_rate"]
            return None  # test done


# ── metadata event hook ────────────────────────────────────────────────────────
# Write run metadata to results dir so CSVs can be joined with context.
@events.test_start.add_listener
def _write_metadata(environment, **kwargs):
    csv_prefix = None
    for i, arg in enumerate(sys.argv):
        if arg == "--csv" and i + 1 < len(sys.argv):
            csv_prefix = sys.argv[i + 1]
            break
    if not csv_prefix:
        return

    results_dir = Path(csv_prefix).parent
    results_dir.mkdir(parents=True, exist_ok=True)
    meta = results_dir / "metadata.yaml"
    meta.write_text(
        f"experiment_id: {EXPERIMENT_ID}\n"
        f"scenario: {SCENARIO}\n"
        f"workload: {WORKLOAD}\n"
        f"intensity: {INTENSITY}\n"
    )
    logging.info("Wrote run metadata to %s", meta)
