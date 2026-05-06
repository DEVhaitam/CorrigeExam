#!/usr/bin/env python3
"""
E4-mem-cap/run.py — runner for E4 (L4 cgroup memory cap, OOM scenario).

Matrix cells: S0-baseline, S8-mem-cap-512m × W2-upload × {medium}

S8 uses W2-upload (PDF uploads) because large PDF buffering in the JVM
pushes total RSS above the cgroup 512m limit, triggering OOM kills.

The JVM heap stays healthy at time of kill (Xmx=2g but only ~300m used)
— the OOM comes from the *container* cgroup, not the JVM heap allocator.
This distinguishes S8 from S1 (JVM heap exhaustion).

Usage:
  python experiments/E4-mem-cap/run.py
  python experiments/E4-mem-cap/run.py --dry-run
"""

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

REPO_ROOT   = Path(__file__).parent.parent.parent
MATRIX_FILE = REPO_ROOT / "experiments" / "matrix.yaml"
RESULTS_DIR = REPO_ROOT / "results"
RUNNER      = REPO_ROOT / "iac" / "stage1-compose" / "run_scenario.sh"
LOCUSTFILE  = REPO_ROOT / "workload" / "locust" / "locustfile.py"
SNAPSHOT_PY = REPO_ROOT / "experiments" / "snapshot.py"

EXPERIMENT_KEY = "E4-mem-cap"
PROM_URL       = os.getenv("PROM_URL", "http://localhost:9092")
BACK_HOST      = os.getenv("BACK_HOST", "http://localhost:8082")


def _load_matrix() -> dict:
    return yaml.safe_load(MATRIX_FILE.read_text())


def _run_cell(
    scenario: str,
    workload: str,
    intensity: str,
    users: int,
    lifecycle: dict,
    dry_run: bool,
) -> None:
    run_id       = str(uuid.uuid4())
    project_name = f"correctexam-{scenario}"
    run_dir      = RESULTS_DIR / run_id
    iac_sha      = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    run_seconds = lifecycle.get("steady_seconds", 300)

    print(f"\n{'='*60}")
    print(f"  experiment:  {EXPERIMENT_KEY}")
    print(f"  run_id:      {run_id}")
    print(f"  scenario:    {scenario}")
    print(f"  workload:    {workload}  intensity={intensity}  users={users}")
    print(f"  run_time:    {run_seconds}s  (steady-state window)")
    print(f"  iac_sha:     {iac_sha}")
    print(f"{'='*60}")

    if dry_run:
        print("  [dry-run] skipping execution")
        return

    run_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "EXPERIMENT_ID": run_id,
        "SCENARIO":      scenario,
        "WORKLOAD":      workload,
        "INTENSITY":     intensity,
        "IAC_SHA":       iac_sha,
    }

    subprocess.run([str(RUNNER), scenario, "up"], check=True, env=env)

    try:
        warmup = lifecycle["warmup_seconds"]
        print(f"[E4] warming up ({warmup}s)...")
        time.sleep(warmup)

        locust_cmd = [
            sys.executable, "-m", "locust",
            "-f", str(LOCUSTFILE),
            "--host", BACK_HOST,
            "--headless",
            "-u", str(users),
            "-r", str(max(1, users // 5)),
            "--run-time", f"{run_seconds}s",
            "--tags", "upload",
            "--csv", str(RESULTS_DIR / run_id / "locust"),
            "--csv-full-history",
        ]
        print(f"[E4] running W2-upload ({run_seconds}s)...")
        start_ts = time.time()
        subprocess.run(locust_cmd, check=False, env=env, cwd=REPO_ROOT)
        end_ts = time.time()

        cooldown = lifecycle["cooldown_seconds"]
        print(f"[E4] cooldown ({cooldown}s)...")
        time.sleep(cooldown)

        subprocess.run(
            [
                sys.executable, str(SNAPSHOT_PY),
                "--run-id",     run_id,
                "--start",      str(start_ts),
                "--end",        str(end_ts),
                "--experiment", EXPERIMENT_KEY,
                "--scenario",   scenario,
                "--workload",   workload,
                "--intensity",  intensity,
                "--iac-sha",    iac_sha,
                "--project-name", project_name,
                "--prom-url",   PROM_URL,
            ],
            check=False, cwd=REPO_ROOT,
        )

    finally:
        subprocess.run([str(RUNNER), scenario, "down"], check=False, env=env)

    print(f"[E4] cell done → results/{run_id}/")


def main() -> None:
    matrix    = _load_matrix()
    exp       = matrix["experiments"][EXPERIMENT_KEY]
    lifecycle = matrix["run_lifecycle"]
    workload  = exp["workloads"][0]   # W2-upload
    intensity_map = matrix["intensity_users"][workload]

    parser = argparse.ArgumentParser(description=f"Runner for {EXPERIMENT_KEY} (L4 memory cap)")
    parser.add_argument("--scenarios",   nargs="+", default=exp["scenarios"])
    parser.add_argument("--intensities", nargs="+", default=exp["intensities"])
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    cells = [
        (s, workload, i)
        for s in args.scenarios
        for i in args.intensities
    ]

    print(f"\n{EXPERIMENT_KEY}: {len(cells)} cells to run")
    for s, w, i in cells:
        print(f"  {s} × {w} × {i}  (users={intensity_map[i]})")

    for scenario, workload_key, intensity in cells:
        users = intensity_map[intensity]
        _run_cell(scenario, workload_key, intensity, users, lifecycle, args.dry_run)

    print(f"\n[E4] all cells done. Results in {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
