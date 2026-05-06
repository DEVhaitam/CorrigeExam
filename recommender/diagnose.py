#!/usr/bin/env python3
"""
diagnose.py — Step 4 of the recommendation loop.

Evaluates diagnostic signals from a snapshot bundle to identify which IaC
layer and knob is responsible for an observed degradation.

Input:  results/<run_id>/   (snapshot bundle produced by snapshot.py)
Output: stdout YAML diagnosis + return code (0=diagnosis found, 1=no match)

Each check implements the "Diagnostic signal" field from EXPERIMENTS.md §3.
The signal name is the key that distinguishes cross-layer traps (e.g. S5 vs S6).

Usage:
  python recommender/diagnose.py --run-id <uuid>
  python recommender/diagnose.py --run-id <uuid> --emit-patch  # pipe to patch.py

Design note: this is a rule-based implementation. It is deliberately simple
and interpretable — the paper's contribution is the *mapping model* and the
*evidence linkage*, not ML sophistication. Each rule returns a confidence in
[0,1] based on how strongly the signal threshold is violated.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT   = Path(__file__).parent.parent
RESULTS_DIR = REPO_ROOT / "results"
MATRIX_FILE = REPO_ROOT / "experiments" / "matrix.yaml"


# ── signal threshold constants (from EXPERIMENTS.md §3) ──────────────────────
GC_PAUSE_RATE_THRESHOLD      = 1.0    # /s — S1
HEAP_RATIO_THRESHOLD         = 0.90   # S1
BUFFER_MISS_RATE_THRESHOLD   = 0.05   # S4 (5% cache miss)
CPU_THROTTLE_THRESHOLD       = 0.50   # /s — S7
MEMORY_FAILCNT_THRESHOLD     = 0      # any > 0 is a failure — S8
AGROAL_AWAIT_THRESHOLD       = 0      # any > 0 sustained — S6
THREAD_PLATEAU_MARGIN        = 0.95   # threads_connected / max_connections — S5


# ── Prometheus data helpers ───────────────────────────────────────────────────

def _load_prom(run_dir: Path) -> dict:
    prom_file = run_dir / "prom_snapshot.json"
    if not prom_file.exists():
        return {}
    return json.loads(prom_file.read_text())


def _get_values(prom: dict, metric_key: str) -> list[float]:
    """Extract all scalar values from a query_range result for a metric."""
    data = prom.get(metric_key, {})
    if data.get("status") != "success":
        return []
    results = data.get("data", {}).get("result", [])
    values = []
    for series in results:
        for _ts, val in series.get("values", []):
            try:
                values.append(float(val))
            except (ValueError, TypeError):
                pass
    return values


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _max(vals: list[float]) -> float:
    return max(vals) if vals else 0.0


def _sustained_above(vals: list[float], threshold: float, fraction: float = 0.5) -> bool:
    """True if >fraction of samples exceed threshold (i.e. 'sustained')."""
    if not vals:
        return False
    return sum(v > threshold for v in vals) / len(vals) >= fraction


# ── individual signal checks ──────────────────────────────────────────────────

def _check_s1_heap(prom: dict) -> dict | None:
    """S1 — JVM heap saturation. L1 knob: JAVA_TOOL_OPTIONS -Xmx."""
    gc_rates = _get_values(prom, "jvm_gc_pause_seconds_count")
    heap_used = _get_values(prom, "jvm_memory_used_bytes{area='heap'}")
    heap_max  = _get_values(prom, "jvm_memory_max_bytes{area='heap'}")

    if not gc_rates or not heap_used or not heap_max:
        return None

    # Approximate GC pause rate from cumulative counter delta
    gc_rate_mean = _mean(gc_rates)  # raw count — rate proxy
    heap_ratio_max = (
        max(u / m for u, m in zip(heap_used, heap_max) if m > 0)
        if heap_max else 0.0
    )

    gc_fires  = _sustained_above(gc_rates, GC_PAUSE_RATE_THRESHOLD, fraction=0.3)
    heap_high = heap_ratio_max > HEAP_RATIO_THRESHOLD

    if not (gc_fires and heap_high):
        return None

    confidence = min(1.0, (heap_ratio_max - HEAP_RATIO_THRESHOLD) * 5 + 0.5)
    return {
        "scenario_match": "S1-heap-256m",
        "layer": "L1",
        "confidence": round(confidence, 2),
        "evidence": [
            {
                "metric": "jvm_gc_pause_seconds_count",
                "value_observed": round(gc_rate_mean, 3),
                "threshold_violated": f"> {GC_PAUSE_RATE_THRESHOLD}/s",
            },
            {
                "metric": "jvm_memory_used / jvm_memory_max (heap)",
                "value_observed": round(heap_ratio_max, 3),
                "threshold_violated": f"> {HEAP_RATIO_THRESHOLD}",
            },
        ],
        "knob": "JAVA_TOOL_OPTIONS",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
    }


def _check_s4_buffer(prom: dict) -> dict | None:
    """S4 — MySQL InnoDB buffer pool cache miss rate. L3 knob: MYSQL_BUFFER_POOL."""
    reads     = _get_values(prom, "mysql_global_status_innodb_buffer_pool_reads")
    read_reqs = _get_values(prom, "mysql_global_status_innodb_buffer_pool_read_requests")

    if not reads or not read_reqs:
        return None

    # Approximate miss rate from cumulative counters
    miss_rates = [
        r / rr for r, rr in zip(reads, read_reqs) if rr > 0
    ]
    if not miss_rates:
        return None

    mean_miss = _mean(miss_rates)
    if mean_miss <= BUFFER_MISS_RATE_THRESHOLD:
        return None

    confidence = min(1.0, mean_miss / BUFFER_MISS_RATE_THRESHOLD * 0.5)
    return {
        "scenario_match": "S4-mysql-buffer-128m",
        "layer": "L3",
        "confidence": round(confidence, 2),
        "evidence": [
            {
                "metric": "innodb_buffer_pool_reads / innodb_buffer_pool_read_requests",
                "value_observed": round(mean_miss, 4),
                "threshold_violated": f"> {BUFFER_MISS_RATE_THRESHOLD} (5% miss rate)",
            }
        ],
        "knob": "MYSQL_BUFFER_POOL",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
    }


def _check_s5_conns(prom: dict) -> dict | None:
    """S5 — MySQL max_connections saturation. L3 knob: MYSQL_MAX_CONNS.
    Key diagnostic: threads_connected plateaus at max_connections value."""
    connected = _get_values(prom, "mysql_global_status_threads_connected")
    max_conns = _get_values(prom, "mysql_global_variables_max_connections")
    aborted   = _get_values(prom, "mysql_global_status_aborted_connects")

    if not connected or not max_conns:
        return None

    ratios = [c / m for c, m in zip(connected, max_conns) if m > 0]
    if not ratios:
        return None

    plateau = _sustained_above(ratios, THREAD_PLATEAU_MARGIN, fraction=0.4)
    aborted_growing = _max(aborted) > 0 if aborted else False

    if not plateau:
        return None

    mean_ratio = _mean(ratios)
    confidence = min(1.0, mean_ratio * 0.9 + (0.1 if aborted_growing else 0))
    return {
        "scenario_match": "S5-mysql-conns-30",
        "layer": "L3",
        "confidence": round(confidence, 2),
        "evidence": [
            {
                "metric": "threads_connected / max_connections",
                "value_observed": round(mean_ratio, 3),
                "threshold_violated": f"> {THREAD_PLATEAU_MARGIN} (plateauing at limit)",
            },
            {
                "metric": "mysql_global_status_aborted_connects",
                "value_observed": _max(aborted) if aborted else "n/a",
                "threshold_violated": "> 0",
            },
        ],
        "knob": "MYSQL_MAX_CONNS",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
        "cross_layer_trap_note": (
            "Distinguish from S6 (Quarkus pool): in S5, DB threads ARE at ceiling. "
            "In S6, DB threads_running is well below capacity — bottleneck is app-side."
        ),
    }


def _check_s6_pool(prom: dict) -> dict | None:
    """S6 — Quarkus Agroal pool exhaustion. L3 knob: QUARKUS_DB_POOL_MAX.
    Cross-layer trap with S5: same user symptom, app-side bottleneck here."""
    awaiting = _get_values(prom, "agroal_awaiting_count")
    active   = _get_values(prom, "agroal_active_count")
    max_used = _get_values(prom, "agroal_max_used_count")
    db_threads = _get_values(prom, "mysql_global_status_threads_running")

    if not awaiting or not active:
        return None

    pool_queued  = _sustained_above(awaiting, AGROAL_AWAIT_THRESHOLD, fraction=0.3)
    db_not_full  = _mean(db_threads) < 10 if db_threads else True  # DB is idle

    if not pool_queued:
        return None

    confidence = min(1.0, _mean(awaiting) * 0.2 + (0.3 if db_not_full else 0))
    return {
        "scenario_match": "S6-quarkus-pool-5",
        "layer": "L3",
        "confidence": round(confidence, 2),
        "evidence": [
            {
                "metric": "agroal_awaiting_count",
                "value_observed": round(_mean(awaiting), 2),
                "threshold_violated": "> 0 (requests waiting for a DB connection)",
            },
            {
                "metric": "mysql_global_status_threads_running",
                "value_observed": round(_mean(db_threads), 2) if db_threads else "n/a",
                "threshold_violated": "far below max_connections (DB has capacity)",
            },
        ],
        "knob": "QUARKUS_DB_POOL_MAX",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
        "cross_layer_trap_note": (
            "Distinguish from S5 (MySQL max_connections): here the DB is NOT at "
            "its connection ceiling — the bottleneck is the app-side Agroal pool."
        ),
    }


def _check_s7_cpu_throttle(prom: dict) -> dict | None:
    """S7 — cgroup CPU throttling. L4 knob: BACK_CPUS (compose resource limit)."""
    throttled = _get_values(
        prom, "container_cpu_cfs_throttled_seconds_total{name='correctexam-back'}"
    )
    if not throttled:
        return None

    sustained = _sustained_above(throttled, CPU_THROTTLE_THRESHOLD, fraction=0.5)
    if not sustained:
        return None

    mean_throttle = _mean(throttled)
    confidence = min(1.0, mean_throttle / CPU_THROTTLE_THRESHOLD * 0.7)
    return {
        "scenario_match": "S7-cpu-cap-half",
        "layer": "L4",
        "confidence": round(confidence, 2),
        "evidence": [
            {
                "metric": "container_cpu_cfs_throttled_seconds_total{name=correctexam-back}",
                "value_observed": round(mean_throttle, 3),
                "threshold_violated": f"> {CPU_THROTTLE_THRESHOLD}/s sustained",
            }
        ],
        "knob": "BACK_CPUS",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
    }


def _check_s8_mem_oom(prom: dict) -> dict | None:
    """S8 — container OOM kill. L4 knob: BACK_MEM (compose memory limit)."""
    failcnt = _get_values(
        prom, "container_memory_failcnt{name='correctexam-back'}"
    )
    restarts = _get_values(
        prom, "container_restarts{name='correctexam-back'}"
    )

    oom_detected = _max(failcnt) > MEMORY_FAILCNT_THRESHOLD if failcnt else False
    restart_detected = _max(restarts) > 0 if restarts else False

    if not (oom_detected or restart_detected):
        return None

    return {
        "scenario_match": "S8-mem-cap-512m",
        "layer": "L4",
        "confidence": 0.9,
        "evidence": [
            {
                "metric": "container_memory_failcnt{name=correctexam-back}",
                "value_observed": _max(failcnt) if failcnt else "n/a",
                "threshold_violated": "> 0 (cgroup OOM events)",
            },
            {
                "metric": "container_restarts{name=correctexam-back}",
                "value_observed": _max(restarts) if restarts else "n/a",
                "threshold_violated": "> 0",
            },
        ],
        "knob": "BACK_MEM",
        "file": "iac/stage1-compose/scenarios/<scenario>.env",
    }


# ── main diagnosis logic ──────────────────────────────────────────────────────

CHECKS = [
    _check_s1_heap,
    _check_s4_buffer,
    _check_s5_conns,
    _check_s6_pool,
    _check_s7_cpu_throttle,
    _check_s8_mem_oom,
]


def diagnose(run_id: str) -> dict:
    run_dir = RESULTS_DIR / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    meta_file = run_dir / "metadata.yaml"
    meta = yaml.safe_load(meta_file.read_text()) if meta_file.exists() else {}

    prom = _load_prom(run_dir)
    if not prom:
        return {
            "run_id": run_id,
            "error": "prom_snapshot.json missing or empty — cannot diagnose",
        }

    hits = []
    for check in CHECKS:
        result = check(prom)
        if result:
            hits.append(result)

    hits.sort(key=lambda h: h["confidence"], reverse=True)

    return {
        "run_id": run_id,
        "scenario_metadata": meta.get("scenario", "unknown"),
        "workload_metadata": meta.get("workload", "unknown"),
        "diagnoses": hits,
        "top_match": hits[0] if hits else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate diagnostic signals for a run.")
    parser.add_argument("--run-id", required=True, help="UUID of the run to diagnose")
    parser.add_argument("--emit-patch", action="store_true",
                        help="If a diagnosis is found, emit a recommendation for patch.py")
    args = parser.parse_args()

    result = diagnose(args.run_id)
    print(yaml.dump(result, sort_keys=False, allow_unicode=True))

    if result.get("top_match") is None:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
