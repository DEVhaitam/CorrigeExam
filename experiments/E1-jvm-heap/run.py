#!/usr/bin/env python3
"""
E1-jvm-heap/run.py — experiment runner for E1 (L1 JVM heap perturbation).

Matrix cell: S0-baseline and S1-heap-256m × W2-upload × {low, medium, high}

For each cell:
  1. Deploy the stack with run_scenario.sh
  2. Warm up 30 s
  3. Run Locust for the steady-state window
  4. Cool down 15 s
  5. Snapshot (Prometheus, GC log, MySQL status)
  6. Tear down

Results land in: results/<run_id>/
Analysis:        experiments/E1-jvm-heap/analyze.ipynb

Usage:
  python experiments/E1-jvm-heap/run.py
  python experiments/E1-jvm-heap/run.py --scenarios S1-heap-256m --intensities medium
  python experiments/E1-jvm-heap/run.py --dry-run          # print plan, no execution
"""

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import yaml

REPO_ROOT    = Path(__file__).parent.parent.parent
MATRIX_FILE  = REPO_ROOT / "experiments" / "matrix.yaml"
SCENARIO_DIR = REPO_ROOT / "iac" / "stage1-compose" / "scenarios"
RESULTS_DIR  = REPO_ROOT / "results"
RUNNER       = REPO_ROOT / "iac" / "stage1-compose" / "run_scenario.sh"
LOCUSTFILE   = REPO_ROOT / "workload" / "locust" / "locustfile.py"
SNAPSHOT_PY  = REPO_ROOT / "experiments" / "snapshot.py"

EXPERIMENT_KEY = "E1-jvm-heap"
PROM_URL       = os.getenv("PROM_URL", "http://localhost:9092")
BACK_HOST      = os.getenv("BACK_HOST", "http://localhost:8082")


def _load_matrix() -> dict:
    return yaml.safe_load(MATRIX_FILE.read_text())


def _locust_args(
    run_id: str,
    scenario: str,
    workload: str,
    intensity: str,
    users: int,
    run_seconds: int,
) -> list[str]:
    csv_prefix = str(RESULTS_DIR / run_id / "locust")
    return [
        sys.executable, "-m", "locust",
        "-f", str(LOCUSTFILE),
        "--host", BACK_HOST,
        "--headless",
        "-u", str(users),
        "-r", str(max(1, users // 10)),
        "--run-time", f"{run_seconds + 30}s",  # +30 s for ramp-up
        "--csv", csv_prefix,
        "--csv-full-history",
    ]


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

    print(f"\n{'='*60}")
    print(f"  experiment:  {EXPERIMENT_KEY}")
    print(f"  run_id:      {run_id}")
    print(f"  scenario:    {scenario}")
    print(f"  workload:    {workload}  intensity={intensity}  users={users}")
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

    # 1. Deploy
    print(f"\n[E1] deploying {scenario}...")
    subprocess.run([str(RUNNER), scenario, "up"], check=True, env=env)

    try:
        # 2. Warmup
        warmup = lifecycle["warmup_seconds"]
        print(f"[E1] warming up ({warmup}s)...")
        time.sleep(warmup)

        # 3. Run Locust
        steady = lifecycle["steady_seconds"]
        locust_cmd = _locust_args(run_id, scenario, workload, intensity, users, steady)
        print(f"[E1] running Locust ({steady}s measurement window)...")
        start_ts = time.time()
        subprocess.run(locust_cmd, check=False, env=env, cwd=REPO_ROOT)
        end_ts = time.time()

        # 4. Cooldown
        cooldown = lifecycle["cooldown_seconds"]
        print(f"[E1] cooldown ({cooldown}s)...")
        time.sleep(cooldown)

        # 5. Snapshot
        subprocess.run(
            [
                sys.executable, str(SNAPSHOT_PY),
                "--run-id",       run_id,
                "--start",        str(start_ts),
                "--end",          str(end_ts),
                "--experiment",   EXPERIMENT_KEY,
                "--scenario",     scenario,
                "--workload",     workload,
                "--intensity",    intensity,
                "--iac-sha",      iac_sha,
                "--project-name", project_name,
                "--prom-url",     PROM_URL,
            ],
            check=False,
            cwd=REPO_ROOT,
        )

    finally:
        # 6. Tear down (always, even on failure)
        print(f"[E1] tearing down {scenario}...")
        subprocess.run([str(RUNNER), scenario, "down"], check=False, env=env)

    print(f"[E1] cell done → results/{run_id}/")


def main() -> None:
    matrix = _load_matrix()
    exp    = matrix["experiments"][EXPERIMENT_KEY]
    lifecycle = matrix["run_lifecycle"]

    parser = argparse.ArgumentParser(description=f"Runner for {EXPERIMENT_KEY}")
    parser.add_argument(
        "--scenarios", nargs="+", default=exp["scenarios"],
        help=f"Scenarios to run (default: {exp['scenarios']})",
    )
    parser.add_argument(
        "--intensities", nargs="+", default=exp["intensities"],
        help=f"Intensities (default: {exp['intensities']})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan only")
    args = parser.parse_args()

    workload = exp["workloads"][0]  # E1 uses W2-upload
    intensity_map = matrix["intensity_users"][workload]

    cells = [
        (scenario, workload, intensity)
        for scenario in args.scenarios
        for intensity in args.intensities
    ]

    print(f"\n{EXPERIMENT_KEY}: {len(cells)} cells to run")
    for s, w, i in cells:
        print(f"  {s} × {w} × {i}  ({intensity_map[i]} users)")

    for scenario, workload_key, intensity in cells:
        users = intensity_map[intensity]
        _run_cell(scenario, workload_key, intensity, users, lifecycle, args.dry_run)

    print(f"\n[E1] all cells done. Results in {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
