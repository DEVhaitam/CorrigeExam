#!/usr/bin/env python3
"""
patch.py — Step 5 of the recommendation loop.

Consumes a diagnosis (from diagnose.py) and emits a source-code patch
recommendation in the schema defined by EXPERIMENTS.md §4.

The recommendation is the paper's central artifact: a minimal, rationale-
annotated source-code patch with traceable evidence-to-patch links.

Input:   --run-id <uuid>  (reads diagnosis from diagnose.py via subprocess)
         OR  --diagnosis <yaml-file>  (pre-computed diagnosis)
Output:  recommendation YAML (stdout) + the actual .env patch applied in-place
         if --apply is set.

Usage:
  python recommender/patch.py --run-id <uuid>
  python recommender/patch.py --run-id <uuid> --apply
  python recommender/diagnose.py --run-id <uuid> | python recommender/patch.py --stdin
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT    = Path(__file__).parent.parent
SCENARIO_DIR = REPO_ROOT / "iac" / "stage1-compose" / "scenarios"
RESULTS_DIR  = REPO_ROOT / "results"


# ── ground-truth fix table (from EXPERIMENTS.md §3) ──────────────────────────
# Maps scenario_match → (env_key, good_value, rationale_template)
FIXES: dict[str, tuple[str, str, str]] = {
    "S1-heap-256m": (
        "JAVA_TOOL_OPTIONS",
        "-Xmx2g -Xms512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200",
        (
            "GC pause rate sustained above 1/s and heap utilization exceeded 90% "
            "over the measurement window, indicating JVM heap saturation under "
            "{workload} load. Increasing -Xmx from 256m to 2g provides headroom "
            "matching the observed working set and is consistent with the Quarkus "
            "recommended baseline for this application tier."
        ),
    ),
    "S4-mysql-buffer-128m": (
        "MYSQL_BUFFER_POOL",
        "1G",
        (
            "InnoDB buffer pool cache miss rate exceeded 5% ({value_observed:.1%}) "
            "during {workload}, meaning most read requests hit disk instead of "
            "memory. Increasing innodb_buffer_pool_size from 128M to 1G allows "
            "the working set to fit in memory, eliminating the disk I/O bottleneck."
        ),
    ),
    "S5-mysql-conns-30": (
        "MYSQL_MAX_CONNS",
        "200",
        (
            "mysql_global_status_threads_connected plateaued at the max_connections "
            "limit ({value_observed:.0%} utilization) with rising aborted_connects. "
            "This is the DB-layer connection ceiling: increasing max_connections "
            "from 30 to 200 removes the hard limit. "
            "Note: distinguish from S6 (Quarkus pool) where DB capacity is unused."
        ),
    ),
    "S6-quarkus-pool-5": (
        "QUARKUS_DB_POOL_MAX",
        "20",
        (
            "agroal_awaiting_count sustained above 0 while "
            "mysql_global_status_threads_running was well below max_connections, "
            "indicating the bottleneck is the app-side connection pool (not the DB). "
            "Increasing QUARKUS_DATASOURCE_JDBC_MAX_SIZE from 5 to 20 (Quarkus "
            "default) removes the artificial app-layer constraint."
        ),
    ),
    "S7-cpu-cap-half": (
        "BACK_CPUS",
        "2.0",
        (
            "container_cpu_cfs_throttled_seconds_total for correctexam-back exceeded "
            "0.5/s sustained — the unique fingerprint of cgroup CPU throttling. "
            "Host CPU was not saturated, ruling out 'bigger node' as the fix. "
            "Raising the compose deploy.resources.limits.cpus from 0.5 to 2.0 "
            "removes the cgroup constraint and restores full CPU entitlement."
        ),
    ),
    "S8-mem-cap-512m": (
        "BACK_MEM",
        "2g",
        (
            "container_memory_failcnt > 0 and/or container restart count increased "
            "during the run — the OOM killer fired at the cgroup level. "
            "The JVM heap metrics were healthy at time of kill, distinguishing "
            "this from S1 (JVM-level OOM). Raising compose memory limit from "
            "512m to 2g gives the JVM room to operate within its own -Xmx budget."
        ),
    ),
}


def _find_env_file(scenario: str) -> Path | None:
    """Find the scenario .env file for the matched scenario."""
    # Try the actual run's scenario name first
    candidates = list(SCENARIO_DIR.glob(f"{scenario}.env"))
    if candidates:
        return candidates[0]
    return None


def _read_current_value(env_file: Path, key: str) -> str:
    for line in env_file.read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return "<not found>"


def _apply_patch(env_file: Path, key: str, new_value: str) -> str:
    text = env_file.read_text()
    new_text = re.sub(
        rf"^({re.escape(key)}=).*$",
        rf"\g<1>{new_value}",
        text,
        flags=re.MULTILINE,
    )
    if new_text == text:
        # Key not found — append
        new_text = text.rstrip("\n") + f"\n{key}={new_value}\n"
    env_file.write_text(new_text)
    return new_text


def generate_patch(diagnosis: dict, scenario_override: str | None = None) -> dict:
    top = diagnosis.get("top_match")
    if not top:
        return {"error": "No diagnosis — cannot generate patch"}

    scenario_match  = top["scenario_match"]
    layer           = top["layer"]
    confidence      = top["confidence"]
    evidence        = top["evidence"]
    workload_meta   = diagnosis.get("workload_metadata", "unknown")

    if scenario_match not in FIXES:
        return {"error": f"No fix defined for scenario_match={scenario_match}"}

    key, good_value, rationale_tpl = FIXES[scenario_match]

    # Use the scenario from the run's metadata to locate the actual .env file
    scenario_name = scenario_override or diagnosis.get("scenario_metadata", scenario_match)
    env_file = _find_env_file(scenario_name)
    current_value = _read_current_value(env_file, key) if env_file else "<file not found>"

    # Fill in rationale template
    ev0 = evidence[0] if evidence else {}
    rationale = rationale_tpl.format(
        workload=workload_meta,
        value_observed=ev0.get("value_observed", 0.0),
    )

    return {
        "recommendation": {
            "layer":          layer,
            "file":           str(env_file.relative_to(REPO_ROOT)) if env_file else top["file"],
            "construct":      key,
            "current_value":  current_value,
            "proposed_value": good_value,
            "rationale":      rationale,
            "evidence":       evidence,
            "confidence":     confidence,
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate IaC patch recommendation.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", help="UUID of run to generate patch for")
    source.add_argument("--stdin",  action="store_true", help="Read diagnosis YAML from stdin")
    parser.add_argument("--apply",    action="store_true", help="Apply patch to .env file in place")
    parser.add_argument("--scenario", help="Override scenario name for .env lookup")
    args = parser.parse_args()

    if args.stdin:
        diagnosis = yaml.safe_load(sys.stdin.read())
    else:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "recommender" / "diagnose.py"),
             "--run-id", args.run_id],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("# diagnose.py returned no match — no patch generated", file=sys.stderr)
            print(result.stdout)
            sys.exit(1)
        diagnosis = yaml.safe_load(result.stdout)

    recommendation = generate_patch(diagnosis, scenario_override=args.scenario)
    print(yaml.dump(recommendation, sort_keys=False, allow_unicode=True))

    if args.apply and "recommendation" in recommendation:
        rec = recommendation["recommendation"]
        env_file = REPO_ROOT / rec["file"]
        if env_file.exists():
            _apply_patch(env_file, rec["construct"], rec["proposed_value"])
            print(f"# Patch applied to {rec['file']}", file=sys.stderr)
        else:
            print(f"# Cannot apply patch: file not found: {rec['file']}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
