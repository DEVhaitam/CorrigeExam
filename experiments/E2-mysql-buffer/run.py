#!/usr/bin/env python3
"""
E2-mysql-buffer/run.py — runner for E2 + E2b (L3 MySQL perturbations).

Matrix cells:
  E2:  S0-baseline, S4-mysql-buffer-128m × W3-grade × {low, medium, high}
  E2b: S0-baseline, S5-mysql-conns-30, S6-quarkus-pool-5 × W3-grade × {medium, high}
       (the cross-layer trap — see EXPERIMENTS.md §3 and PAPERS.md P1)

Usage:
  python experiments/E2-mysql-buffer/run.py
  python experiments/E2-mysql-buffer/run.py --experiment E2b-cross-layer-trap
  python experiments/E2-mysql-buffer/run.py --dry-run
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
RESULTS_DIR  = REPO_ROOT / "results"
RUNNER       = REPO_ROOT / "iac" / "stage1-compose" / "run_scenario.sh"
LOCUSTFILE   = REPO_ROOT / "workload" / "locust" / "locustfile.py"
SNAPSHOT_PY  = REPO_ROOT / "experiments" / "snapshot.py"

DEFAULT_EXPERIMENT = "E2-mysql-buffer"
PROM_URL           = os.getenv("PROM_URL", "http://localhost:9092")
BACK_HOST          = os.getenv("BACK_HOST", "http://localhost:8082")


def _load_matrix() -> dict:
    return yaml.safe_load(MATRIX_FILE.read_text())


def _run_cell(
    experiment_key: str,
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
    print(f"  experiment:  {experiment_key}")
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

    subprocess.run([str(RUNNER), scenario, "up"], check=True, env=env)

    try:
        warmup = lifecycle["warmup_seconds"]
        print(f"[{experiment_key}] warming up ({warmup}s)...")
        time.sleep(warmup)

        steady = lifecycle["steady_seconds"]
        locust_cmd = [
            sys.executable, "-m", "locust",
            "-f", str(LOCUSTFILE),
            "--host", BACK_HOST,
            "--headless",
            "-u", str(users),
            "-r", str(max(1, users // 10)),
            "--run-time", f"{steady + 30}s",
            "--csv", str(RESULTS_DIR / run_id / "locust"),
            "--csv-full-history",
        ]
        print(f"[{experiment_key}] running Locust ({steady}s)...")
        start_ts = time.time()
        subprocess.run(locust_cmd, check=False, env=env, cwd=REPO_ROOT)
        end_ts = time.time()

        cooldown = lifecycle["cooldown_seconds"]
        print(f"[{experiment_key}] cooldown ({cooldown}s)...")
        time.sleep(cooldown)

        subprocess.run(
            [
                sys.executable, str(SNAPSHOT_PY),
                "--run-id",     run_id,
                "--start",      str(start_ts),
                "--end",        str(end_ts),
                "--experiment", experiment_key,
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

    print(f"[{experiment_key}] cell done → results/{run_id}/")


def main() -> None:
    matrix = _load_matrix()
    lifecycle = matrix["run_lifecycle"]

    parser = argparse.ArgumentParser(description="Runner for E2 MySQL experiments")
    parser.add_argument(
        "--experiment",
        choices=["E2-mysql-buffer", "E2b-cross-layer-trap", "all"],
        default=DEFAULT_EXPERIMENT,
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    exp_keys = (
        ["E2-mysql-buffer", "E2b-cross-layer-trap"]
        if args.experiment == "all"
        else [args.experiment]
    )

    for exp_key in exp_keys:
        exp = matrix["experiments"][exp_key]
        workload = exp["workloads"][0]
        intensity_map = matrix["intensity_users"][workload]
        cells = [
            (s, workload, i)
            for s in exp["scenarios"]
            for i in exp["intensities"]
        ]
        print(f"\n{exp_key}: {len(cells)} cells to run")
        for s, w, i in cells:
            print(f"  {s} × {w} × {i}  ({intensity_map[i]} users)")
        for scenario, workload_key, intensity in cells:
            users = intensity_map[intensity]
            _run_cell(exp_key, scenario, workload_key, intensity, users, lifecycle, args.dry_run)

    print(f"\n[E2] all done. Results in {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
