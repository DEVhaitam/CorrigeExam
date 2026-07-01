"""
seed.py — Import testData course bundles into a running CorrectExam instance.

Usage:
    python workload/seed.py                          # import all three files
    python workload/seed.py --files 357 136          # import specific files
    python workload/seed.py --host http://localhost:8082 --user admin --pass admin

The script authenticates as admin, then POSTs each JSON bundle to
/api/importCourse as a multipart 'file' upload — the same shape the UI uses.
It prints a summary of what was imported and exits with a non-zero code if any
import failed.
"""

import argparse
import sys
import time
import logging
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed")

TESTDATA_DIR = Path(__file__).parent.parent / "testData"
FILES = {
    "357": TESTDATA_DIR / "357.json",   # ~85 MB  — small, use for quick seed
    "136": TESTDATA_DIR / "136.json",   # ~134 MB — medium
    "265": TESTDATA_DIR / "265.json",   # ~1 GB   — large, avoid on low RAM
}


def authenticate(host: str, username: str, password: str) -> str:
    url = f"{host}/api/authenticate"
    resp = requests.post(url, json={"username": username, "password": password, "rememberMe": False}, timeout=30)
    resp.raise_for_status()
    token = resp.json()["id_token"]
    log.info("Authenticated as %s", username)
    return token


def import_course(host: str, token: str, path: Path) -> dict:
    url = f"{host}/api/importCourse"
    headers = {"Authorization": f"Bearer {token}"}
    size_mb = path.stat().st_size / 1_000_000
    log.info("Importing %s (%.1f MB) ...", path.name, size_mb)
    t0 = time.time()
    with open(path, "rb") as fh:
        resp = requests.post(
            url,
            headers=headers,
            files={"file": (path.name, fh, "application/json")},
            timeout=600,  # large files take time
        )
    elapsed = time.time() - t0
    if resp.status_code in (200, 201):
        data = resp.json() or {}
        log.info("  OK in %.1fs — course id=%s name=%s", elapsed, data.get("id"), data.get("name"))
        return {"file": path.name, "status": "ok", "elapsed_s": round(elapsed, 1), "course": data}
    else:
        log.error("  FAILED %s in %.1fs: %s", resp.status_code, elapsed, resp.text[:300])
        return {"file": path.name, "status": "failed", "http_status": resp.status_code, "elapsed_s": round(elapsed, 1)}


def main():
    parser = argparse.ArgumentParser(description="Seed CorrectExam with testData bundles")
    parser.add_argument("--host", default="http://localhost:8082")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--pass", dest="password", default="admin")
    parser.add_argument(
        "--files",
        nargs="+",
        choices=list(FILES.keys()),
        default=["357"],
        help="Which testData files to import (default: 357 only — 85 MB, fastest seed)",
    )
    args = parser.parse_args()

    token = authenticate(args.host, args.user, args.password)

    results = []
    for key in args.files:
        path = FILES[key]
        if not path.exists():
            log.error("File not found: %s", path)
            results.append({"file": path.name, "status": "missing"})
            continue
        result = import_course(args.host, token, path)
        results.append(result)

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = len(results) - ok
    log.info("Done: %d imported, %d failed", ok, fail)
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
