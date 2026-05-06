#!/usr/bin/env python3
"""
snapshot.py — collect runtime evidence after a Locust run.

Called by each experiment's run.py once Locust has finished and the
cooldown window has elapsed. Writes a self-contained snapshot bundle to:

  results/<experiment_id>/
    metadata.yaml           — run identifiers (written by locustfile.py on start)
    locust_stats.csv        — per-endpoint stats  (written by locust --csv)
    locust_stats_history.csv — timeseries stats    (written by locust --csv)
    locust_failures.csv     — failure details      (written by locust --csv)
    prom_snapshot.json      — PromQL query_range for all matrix metrics
    gc.log                  — JVM GC log from the back container
    mysql_status.txt        — SHOW ENGINE INNODB STATUS snapshot

Usage (called by run.py, not directly):
    python experiments/snapshot.py \\
        --run-id <uuid> \\
        --start <unix_ts> \\
        --end   <unix_ts> \\
        --scenario S1-heap-256m \\
        --project-name correctexam-S1-heap-256m \\
        --prom-url http://localhost:9092
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

REPO_ROOT   = Path(__file__).parent.parent
MATRIX_FILE = REPO_ROOT / "experiments" / "matrix.yaml"
RESULTS_DIR = REPO_ROOT / "results"

# Prometheus step for query_range (15s matches scrape_interval)
PROM_STEP = "15s"


def _load_metrics() -> list[str]:
    matrix = yaml.safe_load(MATRIX_FILE.read_text())
    return matrix.get("snapshot_metrics", [])


def _prom_query_range(prom_url: str, metric: str, start: float, end: float) -> dict:
    """Fetch a metric's timeseries over [start, end] from Prometheus."""
    url = f"{prom_url}/api/v1/query_range"
    params = {
        "query": metric,
        "start": start,
        "end":   end,
        "step":  PROM_STEP,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "metric": metric, "error": str(exc)}


def collect_prom_snapshot(
    prom_url: str,
    start: float,
    end: float,
    out_path: Path,
) -> None:
    metrics = _load_metrics()
    print(f"  [prom] querying {len(metrics)} metrics ({datetime.fromtimestamp(start, tz=timezone.utc).isoformat()} → {datetime.fromtimestamp(end, tz=timezone.utc).isoformat()})")
    results = {}
    for metric in metrics:
        results[metric] = _prom_query_range(prom_url, metric, start, end)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"  [prom] wrote {out_path.name}")


def collect_gc_log(project_name: str, out_path: Path) -> None:
    """Fetch /tmp/gc.log from the back container (written when -Xlog:gc* is set)."""
    container = f"{project_name}-back-1"
    cmd = ["docker", "exec", container, "cat", "/tmp/gc.log"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            out_path.write_text(result.stdout)
            print(f"  [gc]   wrote {out_path.name} ({len(result.stdout)} bytes)")
        else:
            # GC log may not exist when -Xlog:gc* is not in JAVA_TOOL_OPTIONS
            out_path.write_text(f"# GC log not available: {result.stderr.strip()}\n")
            print(f"  [gc]   log not found in container {container} (no -Xlog:gc* flag?)")
    except Exception as exc:  # noqa: BLE001
        out_path.write_text(f"# Error fetching GC log: {exc}\n")
        print(f"  [gc]   error: {exc}")


def collect_mysql_status(project_name: str, out_path: Path) -> None:
    """Run SHOW ENGINE INNODB STATUS inside the mysql container."""
    container = f"{project_name}-mysql-1"
    cmd = [
        "docker", "exec", container,
        "mysql", "-u", "root", "-prootpassword",
        "-e", "SHOW ENGINE INNODB STATUS\\G",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out_path.write_text(result.stdout or result.stderr)
        print(f"  [db]   wrote {out_path.name}")
    except Exception as exc:  # noqa: BLE001
        out_path.write_text(f"# Error fetching InnoDB status: {exc}\n")
        print(f"  [db]   error: {exc}")


def collect_docker_stats(project_name: str, out_path: Path) -> None:
    """One-shot docker stats snapshot for all project containers."""
    cmd = [
        "docker", "stats",
        "--no-stream", "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        # Filter to this project's containers
        lines = [
            l for l in result.stdout.splitlines()
            if project_name in l or l.startswith("NAME")
        ]
        out_path.write_text("\n".join(lines) + "\n")
        print(f"  [stats] wrote {out_path.name}")
    except Exception as exc:  # noqa: BLE001
        out_path.write_text(f"# Error: {exc}\n")


def write_metadata(
    run_dir: Path,
    run_id: str,
    experiment: str,
    scenario: str,
    workload: str,
    intensity: str,
    iac_sha: str,
) -> None:
    """Write (or overwrite) metadata.yaml so notebooks can discover this run."""
    meta = {
        "run_id":     run_id,
        "experiment": experiment,
        "scenario":   scenario,
        "workload":   workload,
        "intensity":  intensity,
        "iac_sha":    iac_sha,
    }
    (run_dir / "metadata.yaml").write_text(yaml.dump(meta, default_flow_style=False))


def run(
    run_id: str,
    start: float,
    end: float,
    experiment: str,
    scenario: str,
    workload: str,
    intensity: str,
    project_name: str,
    prom_url: str,
    iac_sha: str,
) -> Path:
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[snapshot] collecting evidence for run {run_id}")
    print(f"  scenario={scenario}  window={end - start:.0f}s")

    write_metadata(run_dir, run_id, experiment, scenario, workload, intensity, iac_sha)

    collect_prom_snapshot(prom_url, start, end, run_dir / "prom_snapshot.json")
    collect_gc_log(project_name, run_dir / "gc.log")
    collect_mysql_status(project_name, run_dir / "mysql_status.txt")
    collect_docker_stats(project_name, run_dir / "docker_stats.txt")

    print(f"[snapshot] done → {run_dir}")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect post-run evidence snapshot.")
    parser.add_argument("--run-id",       required=True)
    parser.add_argument("--start",        required=True, type=float, help="Unix timestamp")
    parser.add_argument("--end",          required=True, type=float, help="Unix timestamp")
    parser.add_argument("--experiment",   required=True, help="Experiment key, e.g. E1-jvm-heap")
    parser.add_argument("--scenario",     required=True)
    parser.add_argument("--workload",     required=True)
    parser.add_argument("--intensity",    required=True)
    parser.add_argument("--iac-sha",      default="unknown")
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--prom-url",     default="http://localhost:9092")
    args = parser.parse_args()
    run(
        args.run_id, args.start, args.end,
        args.experiment, args.scenario, args.workload, args.intensity,
        args.project_name, args.prom_url, args.iac_sha,
    )


if __name__ == "__main__":
    main()
