#!/usr/bin/env python3
"""
verify.py — Step 7 of the recommendation loop.

Checks whether a post-patch run has met the pass criteria from
EXPERIMENTS.md §4:

  1. The diagnostic signal from §3 returns within tolerance of baseline.
  2. SLO metric (p99 + error rate) returns to within 1.2× of S0 baseline.
  3. No new diagnostic signal from another scenario starts firing.

Input:
  --baseline-run-id  <uuid>   S0 baseline run to compare against
  --patched-run-id   <uuid>   run after the patch was applied
  --scenario         <str>    e.g. S1-heap-256m (to know which signal to check)

Output: YAML verification report + return code (0=pass, 1=fail, 2=partial)

Usage:
  python recommender/verify.py \\
      --baseline-run-id <uuid-baseline> \\
      --patched-run-id  <uuid-patched> \\
      --scenario S1-heap-256m
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT   = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "results"

# SLO tolerance: post-patch metric must be within this multiple of baseline
SLO_TOLERANCE   = 1.2
# Diagnostic signal tolerance: must drop below threshold × this multiplier
SIGNAL_TOLERANCE = 1.1


# ── load helpers ─────────────────────────────────────────────────────────────

def _load_prom(run_id: str) -> dict:
    p = RESULTS_DIR / run_id / "prom_snapshot.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _get_values(prom: dict, key: str) -> list[float]:
    data = prom.get(key, {})
    if data.get("status") != "success":
        return []
    results = data.get("data", {}).get("result", [])
    vals = []
    for series in results:
        for _, v in series.get("values", []):
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                pass
    return vals


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _max_val(vals: list[float]) -> float:
    return max(vals) if vals else 0.0


def _load_locust_stats(run_id: str) -> dict:
    """Return {name: {p99, error_rate}} from locust_stats.csv."""
    import csv
    stats_file = RESULTS_DIR / run_id / "locust_stats.csv"
    if not stats_file.exists():
        return {}
    rows = {}
    with open(stats_file) as f:
        for row in csv.DictReader(f):
            name = row.get("Name", "")
            try:
                rows[name] = {
                    "p99":        float(row.get("99%", 0) or 0),
                    "error_rate": float(row.get("Failure Count", 0) or 0)
                                  / max(1, float(row.get("Request Count", 1) or 1)),
                }
            except (ValueError, TypeError):
                pass
    return rows


# ── per-scenario signal checks ────────────────────────────────────────────────

def _verify_signal(scenario: str, baseline_prom: dict, patched_prom: dict) -> dict:
    """Return {signal_ok, details} for the scenario's primary diagnostic signal."""

    if scenario == "S1-heap-256m":
        b_heap = _get_values(baseline_prom, "jvm_memory_used_bytes{area='heap'}")
        p_heap = _get_values(patched_prom, "jvm_memory_used_bytes{area='heap'}")
        b_max  = _get_values(baseline_prom, "jvm_memory_max_bytes{area='heap'}")
        p_max  = _get_values(patched_prom, "jvm_memory_max_bytes{area='heap'}")
        b_ratio = _mean([u / m for u, m in zip(b_heap, b_max) if m > 0])
        p_ratio = _mean([u / m for u, m in zip(p_heap, p_max) if m > 0])
        ok = p_ratio <= 0.90 * SIGNAL_TOLERANCE
        return {"signal_ok": ok, "metric": "heap_ratio",
                "baseline": round(b_ratio, 3), "patched": round(p_ratio, 3),
                "threshold": 0.90}

    if scenario == "S4-mysql-buffer-128m":
        b_reads = _get_values(baseline_prom, "mysql_global_status_innodb_buffer_pool_reads")
        p_reads = _get_values(patched_prom, "mysql_global_status_innodb_buffer_pool_reads")
        b_reqs  = _get_values(baseline_prom, "mysql_global_status_innodb_buffer_pool_read_requests")
        p_reqs  = _get_values(patched_prom, "mysql_global_status_innodb_buffer_pool_read_requests")
        b_miss = _mean([r / rr for r, rr in zip(b_reads, b_reqs) if rr > 0])
        p_miss = _mean([r / rr for r, rr in zip(p_reads, p_reqs) if rr > 0])
        ok = p_miss <= 0.01  # <1% miss rate after fix
        return {"signal_ok": ok, "metric": "buffer_miss_rate",
                "baseline": round(b_miss, 4), "patched": round(p_miss, 4),
                "threshold": 0.01}

    if scenario in ("S5-mysql-conns-30", "S6-quarkus-pool-5"):
        key_a = "agroal_awaiting_count"
        key_t = "mysql_global_status_threads_connected"
        b_await = _mean(_get_values(baseline_prom, key_a))
        p_await = _mean(_get_values(patched_prom, key_a))
        ok = p_await == 0
        return {"signal_ok": ok, "metric": key_a,
                "baseline": round(b_await, 2), "patched": round(p_await, 2),
                "threshold": 0}

    if scenario == "S7-cpu-cap-half":
        key = "container_cpu_cfs_throttled_seconds_total{name='correctexam-back'}"
        b_throttle = _mean(_get_values(baseline_prom, key))
        p_throttle = _mean(_get_values(patched_prom, key))
        ok = p_throttle <= 0.1
        return {"signal_ok": ok, "metric": "cpu_throttle_rate",
                "baseline": round(b_throttle, 3), "patched": round(p_throttle, 3),
                "threshold": 0.1}

    if scenario == "S8-mem-cap-512m":
        key = "container_memory_failcnt{name='correctexam-back'}"
        p_failcnt = _max_val(_get_values(patched_prom, key))
        ok = p_failcnt == 0
        return {"signal_ok": ok, "metric": "container_memory_failcnt",
                "baseline": 0, "patched": p_failcnt, "threshold": 0}

    return {"signal_ok": None, "metric": "unknown",
            "note": f"No signal check defined for {scenario}"}


def _verify_slo(baseline_run: str, patched_run: str) -> dict:
    """Compare Locust p99 and error rate between runs. Must be within 1.2×."""
    b_stats = _load_locust_stats(baseline_run)
    p_stats = _load_locust_stats(patched_run)

    checks = {}
    all_pass = True

    for name in set(b_stats) | set(p_stats):
        b = b_stats.get(name, {})
        p = p_stats.get(name, {})
        b_p99 = b.get("p99", 0)
        p_p99 = p.get("p99", 0)
        b_err = b.get("error_rate", 0)
        p_err = p.get("error_rate", 0)

        p99_ok  = (p_p99 <= b_p99 * SLO_TOLERANCE) if b_p99 > 0 else True
        err_ok  = (p_err <= b_err * SLO_TOLERANCE + 0.01)  # allow 1% absolute slack

        checks[name] = {
            "p99_ok": p99_ok,
            "error_rate_ok": err_ok,
            "baseline_p99":  round(b_p99, 3),
            "patched_p99":   round(p_p99, 3),
            "baseline_err":  round(b_err, 4),
            "patched_err":   round(p_err, 4),
        }
        if not (p99_ok and err_ok):
            all_pass = False

    return {"all_pass": all_pass, "endpoints": checks}


def _verify_no_new_signals(scenario: str, patched_prom: dict) -> dict:
    """Condition 3: no new diagnostic signal from a different scenario fires."""
    from recommender.diagnose import CHECKS, _load_prom  # noqa: PLC0415

    new_fires = []
    for check in CHECKS:
        result = check(patched_prom)
        if result and result.get("scenario_match") != scenario:
            new_fires.append(result["scenario_match"])

    return {"no_new_signals": len(new_fires) == 0, "new_fires": new_fires}


# ── main ─────────────────────────────────────────────────────────────────────

def verify(baseline_run_id: str, patched_run_id: str, scenario: str) -> dict:
    baseline_prom = _load_prom(baseline_run_id)
    patched_prom  = _load_prom(patched_run_id)

    signal_result  = _verify_signal(scenario, baseline_prom, patched_prom)
    slo_result     = _verify_slo(baseline_run_id, patched_run_id)
    new_sig_result = _verify_no_new_signals(scenario, patched_prom)

    c1 = signal_result.get("signal_ok", False)
    c2 = slo_result.get("all_pass", False)
    c3 = new_sig_result.get("no_new_signals", False)

    if c1 and c2 and c3:
        verdict, code = "PASS", 0
    elif c1 and c2:
        verdict, code = "PARTIAL (new signal firing)", 2
    else:
        verdict, code = "FAIL", 1

    return {
        "verdict":     verdict,
        "scenario":    scenario,
        "baseline_run": baseline_run_id,
        "patched_run":  patched_run_id,
        "condition_1_signal_normalized": signal_result,
        "condition_2_slo_compliance":    slo_result,
        "condition_3_no_new_signals":    new_sig_result,
    }, code


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify post-patch improvement.")
    parser.add_argument("--baseline-run-id", required=True)
    parser.add_argument("--patched-run-id",  required=True)
    parser.add_argument("--scenario",        required=True)
    args = parser.parse_args()

    result, code = verify(args.baseline_run_id, args.patched_run_id, args.scenario)
    print(yaml.dump(result, sort_keys=False, allow_unicode=True))
    sys.exit(code)


if __name__ == "__main__":
    main()
