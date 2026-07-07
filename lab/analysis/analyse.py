#!/usr/bin/env python3
"""
analyse.py — Generate all experiment diagrams from Locust + Prometheus results.

Produces PNG figures in lab/analysis/figures/:

  Performance (Locust data)
  ─────────────────────────
  Fig 1  — Throughput vs. users: RAM effect  (2-vCPU configs)
  Fig 2  — p95 latency vs. users: RAM effect
  Fig 3  — Throughput vs. users: CPU effect  (4-GB configs)
  Fig 4  — p95 latency vs. users: CPU effect
  Fig 5  — Complete saturation profile for 2cpu-4gb (percentile fan)
  Fig 6  — Response-time percentile bars at u100 across all configs
  Fig 7  — Per-endpoint latency breakdown at two reference loads
  Fig 8  — Throughput heatmap (configs × user counts)
  Fig 9  — Resource efficiency (stable users / vCPU, stable users / GB)
  Fig 10 — Time-series response time + throughput at two load levels
  Fig 11 — Stable user capacity envelope per config
  Fig 12 — Throughput vs. provisioned resources (bubble scatter @ u100)

  System metrics (Prometheus / node-exporter data)
  ─────────────────────────────────────────────────
  Fig 13 — VM CPU % evolution under increasing load (RAM & CPU sweeps)
  Fig 14 — RAM usage: absolute bytes used (proves 2 GB is sufficient)
  Fig 15 — MySQL QPS plateau effect: where the DB connection pool caps out
  Fig 16 — Resource state at capacity limit per config
"""

from __future__ import annotations
import re
import sys
import glob
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import seaborn as sns

# ── paths ─────────────────────────────────────────────────────────────────────
LAB_ROOT = Path(__file__).resolve().parent.parent
RESULTS  = LAB_ROOT / "results"
FIG_DIR  = Path(__file__).resolve().parent / "figures"
FIG_DIR.mkdir(exist_ok=True)

# ── style ─────────────────────────────────────────────────────────────────────
STYLE      = "seaborn-v0_8-whitegrid"
FONT_TITLE = 14
FONT_LABEL = 12
FONT_TICK  = 10
DPI        = 180

# Config metadata: label → (vcpus, ram_gb, display_label)
CONFIGS = {
    "2cpu-2gb": (2, 2, "2 vCPU / 2 GB"),
    "2cpu-4gb": (2, 4, "2 vCPU / 4 GB"),
    "2cpu-8gb": (2, 8, "2 vCPU / 8 GB"),
    "1cpu-4gb": (1, 4, "1 vCPU / 4 GB"),
    "4cpu-4gb": (4, 4, "4 vCPU / 4 GB"),
    "8cpu-4gb": (8, 4, "8 vCPU / 4 GB"),
}

# Colour palettes
RAM_PALETTE = {
    "2cpu-2gb": "#e07b39",
    "2cpu-4gb": "#2b7bb9",
    "2cpu-8gb": "#27ae60",
}
CPU_PALETTE = {
    "1cpu-4gb": "#e07b39",
    "2cpu-4gb": "#2b7bb9",
    "4cpu-4gb": "#9b59b6",
    "8cpu-4gb": "#27ae60",
}

# Configs that hit an infra/time limit before completing the full sweep.
# Capacity numbers from these are lower bounds.
PARTIAL_CONFIGS = {"2cpu-2gb", "2cpu-8gb", "1cpu-4gb"}

# The canonical (latest) run_id to use per config.
# Using the most recent clean run; older 2cpu-4gb runs (20260702) are superseded.
CANONICAL_RUNS = {
    "2cpu-2gb": "20260705_235436",
    "2cpu-4gb": "20260706_011803",
    "2cpu-8gb": "20260706_024427",
    "1cpu-4gb": "20260706_035028",
    "4cpu-4gb": "20260706_045638",
    "8cpu-4gb": "20260706_063236",
}

# ── data loading ───────────────────────────────────────────────────────────────

_STEP_RE = re.compile(r"^u(\d+)_stats\.csv$")


def load_all_stats() -> pd.DataFrame:
    """
    One row per (config, users) from the canonical run of each config.
    Falls back to any available run for configs not in CANONICAL_RUNS.
    """
    rows = []
    for config, run_id in CANONICAL_RUNS.items():
        run_dir = RESULTS / config / run_id
        if not run_dir.exists():
            continue
        for f in run_dir.glob("u*_stats.csv"):
            m = _STEP_RE.match(f.name)
            if not m:
                continue
            users = int(m.group(1))
            df  = pd.read_csv(f)
            agg = df[df["Name"] == "Aggregated"]
            if agg.empty:
                continue
            agg = agg.iloc[0]
            total    = int(agg["Request Count"])
            failures = int(agg["Failure Count"])
            rows.append({
                "config":       config,
                "run_id":       run_id,
                "users":        users,
                "rps":          float(agg["Requests/s"]),
                "failures":     failures,
                "total":        total,
                "failure_rate": failures / total if total else 0.0,
                "p50":          float(agg["50%"]),
                "p75":          float(agg["75%"]),
                "p90":          float(agg["90%"]),
                "p95":          float(agg["95%"]),
                "p99":          float(agg["99%"]),
            })

    df = pd.DataFrame(rows)
    df["flagged"] = False   # no anomalous points in the clean 2026-07-06 runs
    return df.sort_values(["config", "users"]).reset_index(drop=True)


def load_endpoint_stats(config: str, users: int) -> pd.DataFrame | None:
    run_id = CANONICAL_RUNS.get(config)
    if not run_id:
        return None
    f = RESULTS / config / run_id / f"u{users}_stats.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    df = df[df["Name"] != "Aggregated"].copy()
    df["endpoint"] = df["Name"].str.replace(r"^(GET|POST|PUT|DELETE)\s+", "", regex=True)
    return df


def load_history(config: str, users: int) -> pd.DataFrame | None:
    run_id = CANONICAL_RUNS.get(config)
    if not run_id:
        return None
    f = RESULTS / config / run_id / f"u{users}_stats_history.csv"
    if not f.exists():
        return None
    df = pd.read_csv(f)
    df = df[df["Name"] == "Aggregated"].copy()
    t0 = df["Timestamp"].min()
    df["t_sec"] = df["Timestamp"] - t0
    return df


def load_system_data() -> pd.DataFrame:
    """
    One row per (config, users): mean system metrics over that load step's window.
    Metrics from node-exporter, JVM Micrometer, and mysqld-exporter.
    cadvisor (mysql_cpu_pct, back_cpu_pct, …) had no data — excluded.
    """
    rows = []
    for config, run_id in CANONICAL_RUNS.items():
        run_dir = RESULTS / config / run_id
        if not run_dir.exists():
            continue
        for f in sorted(run_dir.glob("u*_system.csv"),
                        key=lambda x: int(x.stem[1:].split("_")[0])):
            users = int(f.stem[1:].split("_")[0])
            df = pd.read_csv(f)
            if df.empty or df["cpu_pct"].isna().all():
                continue
            rows.append({
                "config":              config,
                "users":               users,
                "vcpus":               CONFIGS[config][0],
                "ram_gb":              CONFIGS[config][1],
                "cpu_pct":             df["cpu_pct"].mean(),
                "ram_used_gb":         df["ram_used_bytes"].mean() / 1e9,
                "ram_total_gb":        df["ram_total_bytes"].mean() / 1e9,
                "ram_used_pct":        df["ram_used_bytes"].mean() / df["ram_total_bytes"].mean() * 100,
                "swap_used_gb":        df["swap_used_bytes"].mean() / 1e9,
                "jvm_heap_mb":         df["jvm_heap_bytes"].mean() / 1e6
                                       if df["jvm_heap_bytes"].notna().any() else float("nan"),
                "jvm_nonheap_mb":      df["jvm_nonheap_bytes"].mean() / 1e6
                                       if df["jvm_nonheap_bytes"].notna().any() else float("nan"),
                "mysql_qps":           df["mysql_queries_per_sec"].mean()
                                       if df["mysql_queries_per_sec"].notna().any() else float("nan"),
                "mysql_threads":       df["mysql_threads_connected"].mean()
                                       if df["mysql_threads_connected"].notna().any() else float("nan"),
                "disk_write_mbps":     df["disk_write_bps"].mean() / 1e6,
                "mysql_container_cpu": df["mysql_cpu_pct"].mean()
                                       if "mysql_cpu_pct" in df and df["mysql_cpu_pct"].notna().any() else float("nan"),
                "back_container_cpu":  df["back_cpu_pct"].mean()
                                       if "back_cpu_pct" in df and df["back_cpu_pct"].notna().any() else float("nan"),
                "mysql_container_mem_mb": df["mysql_mem_bytes"].mean() / 1e6
                                       if "mysql_mem_bytes" in df and df["mysql_mem_bytes"].notna().any() else float("nan"),
                "back_container_mem_mb":  df["back_mem_bytes"].mean() / 1e6
                                       if "back_mem_bytes" in df and df["back_mem_bytes"].notna().any() else float("nan"),
            })
    return pd.DataFrame(rows).sort_values(["config", "users"]).reset_index(drop=True)


# ── helpers ────────────────────────────────────────────────────────────────────

def savefig(name: str, tight: bool = True):
    if tight:
        plt.tight_layout()
    path = FIG_DIR / f"{name}.png"
    plt.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close()
    print(f"  ✓  {path.relative_to(LAB_ROOT)}")


def valid(df: pd.DataFrame, config: str) -> pd.DataFrame:
    return df[(df.config == config) & (~df.flagged)].copy()


def stable_capacity(df: pd.DataFrame, config: str,
                    slo_p95: float = 1000.0, max_fail_rate: float = 0.001) -> int:
    """Max users where p95 < slo_p95 ms and failure_rate < max_fail_rate."""
    sub = df[(df.config == config) & (~df.flagged) &
             (df.failure_rate < max_fail_rate) & (df.p95 < slo_p95)]
    return int(sub["users"].max()) if not sub.empty else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 1 — Throughput vs. Users  (RAM axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_throughput_ram(df: pd.DataFrame):
    configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = RAM_PALETTE[cfg]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.rps, "o-", color=color,
                    label=CONFIGS[cfg][2], lw=2, ms=6)
            if cfg in PARTIAL_CONFIGS:
                last = vd.iloc[-1]
                ax.annotate("▶ partial", xy=(last.users, last.rps),
                            xytext=(8, 4), textcoords="offset points",
                            fontsize=8, color=color)

        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("Throughput (req/s)", fontsize=FONT_LABEL)
        ax.set_title("Throughput vs. Concurrent Users — RAM Effect (2 vCPUs)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        ax.yaxis.set_minor_locator(mticker.AutoMinorLocator())
        savefig("fig1_throughput_ram")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 2 — p95 Latency vs. Users  (RAM axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_latency_ram(df: pd.DataFrame):
    configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = RAM_PALETTE[cfg]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.p95, "o-", color=color,
                    label=CONFIGS[cfg][2], lw=2, ms=6)

        ax.axhline(1000, ls="--", color="red", lw=1.2, alpha=0.7, label="1 s SLO")
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("p95 Response Time (ms)", fontsize=FONT_LABEL)
        ax.set_title("p95 Latency vs. Concurrent Users — RAM Effect (2 vCPUs)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig2_latency_ram")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 3 — Throughput vs. Users  (CPU axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_throughput_cpu(df: pd.DataFrame):
    configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = CPU_PALETTE[cfg]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.rps, "o-", color=color,
                    label=CONFIGS[cfg][2], lw=2, ms=6)
            if cfg in PARTIAL_CONFIGS:
                last = vd.iloc[-1]
                ax.annotate("▶ partial", xy=(last.users, last.rps),
                            xytext=(8, 4), textcoords="offset points",
                            fontsize=8, color=color)

        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("Throughput (req/s)", fontsize=FONT_LABEL)
        ax.set_title("Throughput vs. Concurrent Users — CPU Effect (4 GB RAM)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig3_throughput_cpu")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 4 — p95 Latency vs. Users  (CPU axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_latency_cpu(df: pd.DataFrame):
    configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = CPU_PALETTE[cfg]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.p95, "o-", color=color,
                    label=CONFIGS[cfg][2], lw=2, ms=6)

        ax.axhline(1000, ls="--", color="red", lw=1.2, alpha=0.7, label="1 s SLO")
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("p95 Response Time (ms)", fontsize=FONT_LABEL)
        ax.set_title("p95 Latency vs. Concurrent Users — CPU Effect (4 GB RAM)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.yaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig4_latency_cpu")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 5 — Complete saturation profile for 2cpu-4gb  (percentile fan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_saturation_2cpu4gb(df: pd.DataFrame):
    d = valid(df, "2cpu-4gb")
    with plt.style.context(STYLE):
        fig, axes = plt.subplots(2, 1, figsize=(9, 8), sharex=True)

        # top — throughput
        axes[0].plot(d.users, d.rps, "o-", color="#2b7bb9", lw=2.5, ms=7)
        axes[0].fill_between(d.users, 0, d.rps, color="#2b7bb9", alpha=0.1)
        axes[0].set_ylabel("Throughput (req/s)", fontsize=FONT_LABEL)
        axes[0].set_title("Saturation Profile — 2 vCPU / 4 GB RAM",
                          fontsize=FONT_TITLE, fontweight="bold")
        knee_idx = d["rps"].idxmax()
        axes[0].annotate(
            f"Peak {d.loc[knee_idx,'rps']:.0f} req/s\n@ {d.loc[knee_idx,'users']} users",
            xy=(d.loc[knee_idx, "users"], d.loc[knee_idx, "rps"]),
            xytext=(d.loc[knee_idx, "users"] * 0.3, d.loc[knee_idx, "rps"] * 0.85),
            arrowprops=dict(arrowstyle="->", color="grey"),
            fontsize=FONT_TICK, color="#2b7bb9",
        )

        # bottom — percentile fan
        axes[1].fill_between(d.users, d.p50, d.p95, alpha=0.25,
                              color="#e07b39", label="p50–p95 band")
        axes[1].plot(d.users, d.p50, "o-",  color="#27ae60", lw=2,   ms=5, label="p50")
        axes[1].plot(d.users, d.p75, "s--", color="#f39c12", lw=1.5, ms=4, label="p75")
        axes[1].plot(d.users, d.p95, "^-",  color="#e07b39", lw=2,   ms=5, label="p95")
        axes[1].plot(d.users, d.p99, "D--", color="#c0392b", lw=1.5, ms=4, label="p99")
        axes[1].axhline(1000, ls=":", color="red", lw=1.2, alpha=0.7, label="1 s SLO")
        axes[1].set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        axes[1].set_ylabel("Response Time (ms)", fontsize=FONT_LABEL)
        axes[1].set_yscale("log")
        axes[1].yaxis.set_major_formatter(mticker.ScalarFormatter())
        axes[1].legend(fontsize=FONT_TICK, loc="upper left")

        for ax in axes:
            ax.set_xscale("log")
            ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
            ax.tick_params(labelsize=FONT_TICK)

        plt.subplots_adjust(hspace=0.08)
        savefig("fig5_saturation_2cpu4gb", tight=False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 6 — Response-time percentile bars across configs at u100
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_percentile_bars(df: pd.DataFrame):
    target_u = 100
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    pcts       = ["p50", "p75", "p90", "p95", "p99"]
    pct_colors = ["#27ae60", "#f1c40f", "#e67e22", "#e07b39", "#c0392b"]

    sub = df[(df.users == target_u) & (~df.flagged) & df.config.isin(configs_order)].copy()
    sub = sub.set_index("config").reindex([c for c in configs_order if c in sub.index])

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(10, 5))
        x     = np.arange(len(sub))
        width = 0.14
        for i, (pct, col) in enumerate(zip(pcts, pct_colors)):
            offset = (i - 2) * width
            ax.bar(x + offset, sub[pct].values, width * 0.92,
                   color=col, label=pct, zorder=3)

        ax.set_xticks(x)
        ax.set_xticklabels([CONFIGS[c][2] for c in sub.index],
                           fontsize=FONT_TICK)
        ax.set_ylabel("Response Time (ms)", fontsize=FONT_LABEL)
        ax.set_title(f"Response-Time Percentiles at {target_u} Concurrent Users",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.legend(title="Percentile", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig6_percentile_bars")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 7 — Endpoint latency breakdown for 2cpu-4gb at u500 and u1000
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_endpoint_breakdown(df: pd.DataFrame):
    pairs = [("2cpu-4gb", 500), ("2cpu-4gb", 1000)]
    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=False)
        for ax, (cfg, users) in zip(axes, pairs):
            ep = load_endpoint_stats(cfg, users)
            if ep is None:
                ax.text(0.5, 0.5, "No data", ha="center", va="center")
                continue
            ep = ep.sort_values("95%", ascending=False).head(12)
            short = [
                n.replace("GET ", "").replace("POST ", "").replace("PUT ", "")
                 .replace("/api/", "")[:30]
                for n in ep["endpoint"]
            ]
            y = np.arange(len(ep))
            ax.barh(y, ep["95%"].values, color="#2b7bb9", alpha=0.85, label="p95", zorder=3)
            ax.barh(y, ep["50%"].values, color="#27ae60", alpha=0.85, label="p50", zorder=4)
            ax.set_yticks(y)
            ax.set_yticklabels(short, fontsize=8)
            ax.set_xlabel("Response Time (ms)", fontsize=FONT_LABEL)
            ax.set_title(f"2 vCPU / 4 GB — {users} users",
                         fontsize=FONT_LABEL, fontweight="bold")
            ax.legend(fontsize=FONT_TICK)
            ax.tick_params(labelsize=FONT_TICK)

        fig.suptitle("Per-Endpoint p50 / p95 Latency (top 12 slowest)",
                     fontsize=FONT_TITLE, fontweight="bold", y=1.01)
        savefig("fig7_endpoint_breakdown")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 8 — Throughput heatmap  (configs × user counts)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_heatmap(df: pd.DataFrame):
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    d     = df[~df.flagged & df.config.isin(configs_order)].copy()
    pivot = d.pivot_table(index="config", columns="users", values="rps", aggfunc="mean")
    pivot = pivot.reindex(configs_order)
    row_labels = [CONFIGS[c][2] for c in pivot.index]

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(14, 4))
        sns.heatmap(
            pivot, ax=ax, mask=pivot.isna(),
            cmap="YlOrRd", annot=True, fmt=".0f",
            linewidths=0.5, linecolor="white",
            cbar_kws={"label": "Throughput (req/s)", "shrink": 0.8},
        )
        ax.set_yticklabels(row_labels, rotation=0, fontsize=FONT_TICK)
        ax.set_xticklabels([str(c) for c in pivot.columns], fontsize=FONT_TICK)
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("VM Configuration", fontsize=FONT_LABEL)
        ax.set_title(
            "Throughput Heatmap — req/s by Configuration and Load Level\n"
            "(grey = not tested / run stopped before reaching that load)",
            fontsize=FONT_TITLE, fontweight="bold",
        )
        savefig("fig8_heatmap")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 9 — Resource efficiency  (stable users / vCPU and / GB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_efficiency(df: pd.DataFrame):
    cpu_configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    ram_configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]

    rows = []
    for cfg in set(cpu_configs + ram_configs):
        su = stable_capacity(df, cfg)
        rows.append({
            "config":  cfg,
            "label":   CONFIGS[cfg][2],
            "vcpus":   CONFIGS[cfg][0],
            "ram":     CONFIGS[cfg][1],
            "stable":  su,
            "partial": cfg in PARTIAL_CONFIGS,
        })
    edf = pd.DataFrame(rows)

    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(13, 5))

        def _bars(ax, sub, sort_col, divisor_col, color_fn, title):
            sub = sub.sort_values(sort_col).copy()
            sub["y"] = sub["stable"] / sub[divisor_col]
            for i, (_, row) in enumerate(sub.iterrows()):
                ax.bar(i, row["y"], color=color_fn(row["config"]),
                       hatch="//" if row["partial"] else "",
                       edgecolor="white", zorder=3)
                prefix = "≥" if row["partial"] else ""
                ax.text(i, row["y"] + 0.5, f"{prefix}{row['y']:.0f}",
                        ha="center", va="bottom",
                        fontsize=FONT_TICK, fontweight="bold")
            ax.set_xticks(range(len(sub)))
            ax.set_xticklabels(sub["label"].tolist(), rotation=15, fontsize=FONT_TICK)
            ax.set_ylim(0, sub["y"].max() * 1.35)
            ax.set_title(title, fontsize=FONT_LABEL, fontweight="bold")
            ax.tick_params(axis="y", labelsize=FONT_TICK)

        _bars(axes[0], edf[edf.config.isin(cpu_configs)],
              "vcpus", "vcpus",
              lambda c: CPU_PALETTE.get(c, "#888"),
              "CPU Efficiency\n(stable users / vCPU @ 4 GB RAM)")
        axes[0].set_ylabel("Stable Users per vCPU", fontsize=FONT_LABEL)

        _bars(axes[1], edf[edf.config.isin(ram_configs)],
              "ram", "ram",
              lambda c: RAM_PALETTE.get(c, "#888"),
              "RAM Efficiency\n(stable users / GB @ 2 vCPUs)")
        axes[1].set_ylabel("Stable Users per GB RAM", fontsize=FONT_LABEL)

        solid_p = mpatches.Patch(facecolor="grey", label="Fully tested")
        hatch_p = mpatches.Patch(facecolor="grey", hatch="//",
                                  edgecolor="white", label="Partial — lower bound")
        for ax in axes:
            ax.legend(handles=[solid_p, hatch_p], fontsize=8)

        fig.suptitle("Resource Provisioning Efficiency\n"
                     "(max stable concurrent users per provisioned resource unit)",
                     fontsize=FONT_TITLE, fontweight="bold")
        savefig("fig9_efficiency")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 10 — Time-series response time for 2cpu-4gb at u500 and u1000
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_timeseries(df: pd.DataFrame):
    pairs = [("2cpu-4gb", 500, "#2b7bb9"), ("2cpu-4gb", 1000, "#c0392b")]
    with plt.style.context(STYLE):
        fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=False)
        for ax, (cfg, users, col) in zip(axes, pairs):
            hist = load_history(cfg, users)
            if hist is None or hist.empty:
                ax.text(0.5, 0.5, "No history data", ha="center", va="center",
                        transform=ax.transAxes)
                continue
            hist = hist[hist["User Count"] > 0].copy()
            ax.fill_between(hist.t_sec, hist["50%"], hist["95%"],
                            alpha=0.2, color=col, label="p50–p95")
            ax.plot(hist.t_sec, hist["50%"], color=col, lw=1.5, label="p50")
            ax.plot(hist.t_sec, hist["95%"], color=col, lw=1.5, ls="--", label="p95")
            ax2 = ax.twinx()
            ax2.plot(hist.t_sec, hist["Requests/s"], color="grey",
                     lw=1.2, ls=":", alpha=0.7)
            ax2.set_ylabel("req/s", fontsize=9, color="grey")
            ax2.tick_params(labelsize=8, colors="grey")
            ax.axhline(1000, ls=":", color="red", lw=1, alpha=0.6)
            ax.set_ylabel("Response Time (ms)", fontsize=FONT_LABEL)
            ax.set_title(f"2 vCPU / 4 GB — {users} concurrent users",
                         fontsize=FONT_LABEL, fontweight="bold")
            ax.legend(fontsize=FONT_TICK, loc="upper left")
            ax.tick_params(labelsize=FONT_TICK)
            ax.set_xlabel("Elapsed Time (s)", fontsize=FONT_LABEL)

        fig.suptitle("Response-Time Time Series (p50 / p95) + Throughput",
                     fontsize=FONT_TITLE, fontweight="bold")
        plt.subplots_adjust(hspace=0.38)
        savefig("fig10_timeseries", tight=False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 11 — Stable user capacity envelope per config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_stable_envelope(df: pd.DataFrame):
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    data = []
    for cfg in configs_order:
        su = stable_capacity(df, cfg)
        data.append({
            "config":  cfg,
            "label":   CONFIGS[cfg][2],
            "stable":  su,
            "partial": cfg in PARTIAL_CONFIGS,
        })
    edf = pd.DataFrame(data)

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(9, 5))
        colors = [RAM_PALETTE.get(c, CPU_PALETTE.get(c, "#888")) for c in edf.config]

        for i, (_, row) in enumerate(edf.iterrows()):
            ax.bar(row["label"], row["stable"],
                   color=colors[i],
                   hatch="//" if row["partial"] else "",
                   edgecolor="white", linewidth=0.8, zorder=3)
            prefix = "≥" if row["partial"] else ""
            ax.text(i, row["stable"] + 8, f"{prefix}{row['stable']}",
                    ha="center", va="bottom",
                    fontsize=FONT_TICK + 1, fontweight="bold")

        ax.set_ylabel("Max Stable Users (p95 < 1 s, failure rate < 0.1%)",
                      fontsize=FONT_LABEL)
        ax.set_title("Stable Capacity Envelope per VM Configuration",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.tick_params(axis="x", rotation=18, labelsize=FONT_TICK)
        ax.tick_params(axis="y", labelsize=FONT_TICK)
        ax.set_ylim(0, edf["stable"].max() * 1.22)

        solid_p = mpatches.Patch(facecolor="grey", label="Fully tested")
        hatch_p = mpatches.Patch(facecolor="grey", hatch="//",
                                  edgecolor="white",
                                  label="Partial run — value is a lower bound")
        ax.legend(handles=[solid_p, hatch_p], fontsize=FONT_TICK, loc="upper right")
        savefig("fig11_stable_envelope")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 12 — Throughput vs. provisioned resources (bubble scatter @ u100)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_scatter_resources(df: pd.DataFrame):
    target_u = 100
    sub = df[(df.users == target_u) & (~df.flagged)].copy()
    sub = sub.drop_duplicates("config")
    sub["vcpus"] = sub.config.map(lambda c: CONFIGS[c][0])
    sub["ram"]   = sub.config.map(lambda c: CONFIGS[c][1])

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(7, 5))
        sc = ax.scatter(sub["vcpus"], sub["ram"], s=sub["rps"] * 4,
                        c=sub["rps"], cmap="YlOrRd", zorder=4,
                        edgecolors="grey", linewidths=0.6)
        for _, row in sub.iterrows():
            ax.annotate(f"{row['rps']:.0f} r/s",
                        (row.vcpus, row.ram),
                        textcoords="offset points", xytext=(8, 4), fontsize=8)
        plt.colorbar(sc, ax=ax, label="Throughput (req/s)")
        ax.set_xlabel("vCPUs", fontsize=FONT_LABEL)
        ax.set_ylabel("RAM (GB)", fontsize=FONT_LABEL)
        ax.set_xticks([1, 2, 4, 8])
        ax.set_yticks([2, 4, 8])
        ax.set_title(f"Throughput vs. Provisioned Resources @ {target_u} Users\n"
                     f"(bubble size ∝ req/s)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig12_scatter_resources")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 13 — VM CPU % evolution under increasing load  (RAM & CPU sweep panels)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_cpu_evolution(sdf: pd.DataFrame):
    """
    Two side-by-side panels: left = RAM sweep (2-vCPU configs, x=users, y=CPU%),
    right = CPU sweep (4-GB configs).
    A 90% saturation threshold line is drawn to show when each config "saturates".
    The slope of each curve reveals how quickly the CPU becomes the bottleneck.
    """
    ram_cfgs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]
    cpu_cfgs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]

    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        for ax, cfgs, palette, title in [
            (axes[0], ram_cfgs, RAM_PALETTE,
             "CPU Utilisation — RAM Sweep (2 vCPUs)"),
            (axes[1], cpu_cfgs, CPU_PALETTE,
             "CPU Utilisation — CPU Sweep (4 GB RAM)"),
        ]:
            ax.axhline(90, ls="--", color="red", lw=1.2, alpha=0.7,
                       label="90% saturation threshold")
            for cfg in cfgs:
                d = sdf[sdf.config == cfg].sort_values("users")
                if d.empty:
                    continue
                color = palette.get(cfg, "#888")
                ax.plot(d.users, d.cpu_pct, "o-", color=color,
                        label=CONFIGS[cfg][2], lw=2, ms=6)
                if cfg in PARTIAL_CONFIGS:
                    last = d.iloc[-1]
                    ax.annotate("▶", xy=(last.users, last.cpu_pct),
                                xytext=(6, 0), textcoords="offset points",
                                fontsize=11, color=color)

            ax.set_xscale("log")
            ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
            ax.set_ylim(0, 105)
            ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
            ax.set_ylabel("VM CPU Utilisation (%)", fontsize=FONT_LABEL)
            ax.set_title(title, fontsize=FONT_LABEL, fontweight="bold")
            ax.legend(fontsize=FONT_TICK)
            ax.tick_params(labelsize=FONT_TICK)

        fig.suptitle(
            "CPU Saturation Under Load\n"
            "All 2-vCPU configs converge to the same curve (RAM is not the bottleneck).\n"
            "1 vCPU saturates at u200; 4/8 vCPU still have headroom at u4000.",
            fontsize=FONT_TITLE, fontweight="bold",
        )
        savefig("fig13_cpu_evolution")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 14 — RAM usage: absolute bytes used  (proves 2 GB is sufficient)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_ram_pressure(sdf: pd.DataFrame):
    """
    Two panels.
    Left: absolute RAM used (GB) per 2-vCPU config across load steps.
      All three lines should overlap → they all consume the same ~1.4-2 GB
      regardless of allocation, proving 2 GB is the right minimum.
    Right: RAM used % (fills remaining capacity) — shows the 2 GB config is
      running at 67-74% headroom while 8 GB sits at 20-23%.
    """
    ram_cfgs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]

    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        # left — absolute GB used
        ax = axes[0]
        for cfg in ram_cfgs:
            d = sdf[sdf.config == cfg].sort_values("users")
            if d.empty:
                continue
            ax.plot(d.users, d.ram_used_gb, "o-", color=RAM_PALETTE[cfg],
                    label=CONFIGS[cfg][2], lw=2, ms=6)

        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("RAM Used (GB)", fontsize=FONT_LABEL)
        ax.set_title("Absolute RAM Consumption\n"
                     "(all 2-vCPU configs use the same absolute amount)",
                     fontsize=FONT_LABEL, fontweight="bold")
        ax.legend(fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)

        # right — % utilisation
        ax = axes[1]
        for cfg in ram_cfgs:
            d = sdf[sdf.config == cfg].sort_values("users")
            if d.empty:
                continue
            ax.plot(d.users, d.ram_used_pct, "o-", color=RAM_PALETTE[cfg],
                    label=CONFIGS[cfg][2], lw=2, ms=6)
            # annotate the allocation size
            last = d.iloc[-1]
            ax.annotate(f"{CONFIGS[cfg][1]} GB alloc.",
                        xy=(last.users, last.ram_used_pct),
                        xytext=(6, 0), textcoords="offset points",
                        fontsize=8, color=RAM_PALETTE[cfg])

        ax.axhline(80, ls="--", color="red", lw=1.2, alpha=0.7,
                   label="80% pressure threshold")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("RAM Used (%)", fontsize=FONT_LABEL)
        ax.set_ylim(0, 100)
        ax.set_title("RAM Utilisation %\n"
                     "(2 GB runs at 68-74%; 4 GB is the comfortable sweet spot)",
                     fontsize=FONT_LABEL, fontweight="bold")
        ax.legend(fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)

        fig.suptitle(
            "RAM Pressure Analysis — 2-vCPU Configs\n"
            "The app consumes ~1.4-2.0 GB regardless of allocation; "
            "2 GB leaves very little headroom.",
            fontsize=FONT_TITLE, fontweight="bold",
        )
        savefig("fig14_ram_pressure")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 15 — MySQL QPS plateau: DB connection pool caps throughput at ≥4 vCPUs
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_mysql_plateau(sdf: pd.DataFrame, ldf: pd.DataFrame):
    """
    Left: MySQL QPS vs concurrent users, per config (CPU sweep).
      Shows 4-cpu and 8-cpu plateau at ~4 000–5 000 QPS while 2-cpu saturates
      at ~2 300 QPS — the difference explains CPU scaling.
    Right: Scatter of Locust throughput (RPS) vs MySQL QPS per (config, users).
      A linear relationship means DB scales with HTTP load.
      The 4/8-cpu points diverge at high load → DB becomes the bottleneck.
    """
    cpu_cfgs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]

    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        # left — MySQL QPS evolution
        ax = axes[0]
        for cfg in cpu_cfgs:
            d = sdf[sdf.config == cfg].sort_values("users")
            if d.empty:
                continue
            ax.plot(d.users, d.mysql_qps, "o-", color=CPU_PALETTE[cfg],
                    label=CONFIGS[cfg][2], lw=2, ms=6)

        ax.axhline(4800, ls="--", color="grey", lw=1.2, alpha=0.7,
                   label="~4 800 QPS ceiling (9-connection pool)")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("MySQL Queries per Second", fontsize=FONT_LABEL)
        ax.set_title("MySQL QPS vs Load\n"
                     "4/8-vCPU configs plateau near ~4 800 QPS",
                     fontsize=FONT_LABEL, fontweight="bold")
        ax.legend(fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)

        # right — Locust RPS vs MySQL QPS scatter
        ax = axes[1]
        merged = pd.merge(
            ldf[ldf.config.isin(cpu_cfgs) & ~ldf.flagged][
                ["config", "users", "rps"]],
            sdf[sdf.config.isin(cpu_cfgs)][
                ["config", "users", "mysql_qps"]],
            on=["config", "users"],
        ).dropna()

        for cfg in cpu_cfgs:
            d = merged[merged.config == cfg].sort_values("rps")
            if d.empty:
                continue
            ax.scatter(d.mysql_qps, d.rps, color=CPU_PALETTE[cfg],
                       label=CONFIGS[cfg][2], s=50, zorder=4, alpha=0.85)
            # label the highest-load point
            last = d.iloc[-1]
            ax.annotate(f"u{last.users}",
                        xy=(last.mysql_qps, last.rps),
                        xytext=(4, 4), textcoords="offset points",
                        fontsize=7, color=CPU_PALETTE[cfg])

        # reference line for linear relationship
        xmax = merged["mysql_qps"].max()
        slope = merged["rps"].max() / merged["mysql_qps"].max()
        xs = np.linspace(0, xmax, 100)
        ax.plot(xs, slope * xs, "--", color="grey", lw=1, alpha=0.5,
                label="linear reference")

        ax.set_xlabel("MySQL QPS", fontsize=FONT_LABEL)
        ax.set_ylabel("HTTP Throughput (req/s)", fontsize=FONT_LABEL)
        ax.set_title("HTTP Throughput vs. MySQL QPS\n"
                     "Divergence at high load = DB becomes the bottleneck",
                     fontsize=FONT_LABEL, fontweight="bold")
        ax.legend(fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)

        fig.suptitle(
            "MySQL Connection-Pool Ceiling (9 connections)\n"
            "At ≥4 vCPUs the DB pool saturates before the CPUs do, "
            "explaining diminishing returns from adding more cores.",
            fontsize=FONT_TITLE, fontweight="bold",
        )
        savefig("fig15_mysql_plateau")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIG 16 — Resource state at capacity limit per config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_resource_at_limit(sdf: pd.DataFrame, ldf: pd.DataFrame):
    """
    For each config, take the last load step where failure_rate < 0.1% (the
    capacity limit).  Plot a grouped bar: CPU%, RAM%, and MySQL QPS as a %
    of the measured ceiling (~4 800 QPS).  This directly answers: WHAT is
    exhausted when the config reaches its limit?
    """
    configs_order = ["1cpu-4gb", "2cpu-4gb", "2cpu-8gb", "2cpu-2gb",
                     "4cpu-4gb", "8cpu-4gb"]
    MYSQL_CEIL = 4800.0

    rows = []
    for cfg in configs_order:
        # last "good" locust step
        ld = ldf[(ldf.config == cfg) & (ldf.failure_rate < 0.001)
                 & (~ldf.flagged)].copy()
        if ld.empty:
            continue
        last_u = ld["users"].max()
        sd = sdf[(sdf.config == cfg) & (sdf.users == last_u)]
        if sd.empty:
            # use closest available system step
            sd = sdf[sdf.config == cfg].sort_values("users")
            if sd.empty:
                continue
            sd = sd.iloc[[-1]]
        sd = sd.iloc[0]

        rows.append({
            "label":     CONFIGS[cfg][2],
            "users":     last_u,
            "cpu_pct":   sd["cpu_pct"],
            "ram_pct":   sd["ram_used_pct"],
            "mysql_pct": min(sd["mysql_qps"] / MYSQL_CEIL * 100, 100),
            "partial":   cfg in PARTIAL_CONFIGS,
        })

    rdf = pd.DataFrame(rows)
    x   = np.arange(len(rdf))
    w   = 0.25

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(11, 5))

        b1 = ax.bar(x - w, rdf["cpu_pct"],   w * 0.9, label="CPU %",        color="#e07b39", zorder=3)
        b2 = ax.bar(x,     rdf["ram_pct"],   w * 0.9, label="RAM %",        color="#2b7bb9", zorder=3)
        b3 = ax.bar(x + w, rdf["mysql_pct"], w * 0.9, label="MySQL QPS %\n(of 4 800 ceiling)", color="#9b59b6", zorder=3)

        ax.axhline(90, ls="--", color="red", lw=1.2, alpha=0.6, label="90% threshold")

        for bars in (b1, b2, b3):
            for bar in bars:
                h = bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2, h + 0.8,
                        f"{h:.0f}", ha="center", va="bottom", fontsize=7)

        ax.set_xticks(x)
        ax.set_xticklabels(
            [f"{r['label']}\n(u{r['users']})" + (" ▶" if r["partial"] else "")
             for _, r in rdf.iterrows()],
            fontsize=FONT_TICK,
        )
        ax.set_ylim(0, 115)
        ax.set_ylabel("Resource Utilisation (%)", fontsize=FONT_LABEL)
        ax.set_title(
            "Resource State at Capacity Limit per Configuration\n"
            "(load step = last step with < 0.1% failures; ▶ = partial run)",
            fontsize=FONT_TITLE, fontweight="bold",
        )
        ax.legend(fontsize=FONT_TICK, loc="upper right")
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig16_resource_at_limit")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print("Loading Locust results …")
    ldf = load_all_stats()
    print(f"  {len(ldf)} (config, users) data points across "
          f"{ldf.config.nunique()} configurations")
    print(f"  Configs: {sorted(ldf.config.unique())}")
    print(f"  Users:   {sorted(ldf.users.unique())}\n")

    print("Loading system metrics …")
    sdf = load_system_data()
    print(f"  {len(sdf)} (config, users) system-metric rows\n")

    print(f"Output → {FIG_DIR}\n")

    steps = [
        ("Fig  1  — Throughput RAM effect",            lambda: fig_throughput_ram(ldf)),
        ("Fig  2  — Latency RAM effect",               lambda: fig_latency_ram(ldf)),
        ("Fig  3  — Throughput CPU effect",            lambda: fig_throughput_cpu(ldf)),
        ("Fig  4  — Latency CPU effect",               lambda: fig_latency_cpu(ldf)),
        ("Fig  5  — Saturation profile 2cpu-4gb",      lambda: fig_saturation_2cpu4gb(ldf)),
        ("Fig  6  — Percentile bars @ u100",           lambda: fig_percentile_bars(ldf)),
        ("Fig  7  — Endpoint breakdown",               lambda: fig_endpoint_breakdown(ldf)),
        ("Fig  8  — Throughput heatmap",               lambda: fig_heatmap(ldf)),
        ("Fig  9  — Resource efficiency",              lambda: fig_efficiency(ldf)),
        ("Fig 10  — Time-series",                      lambda: fig_timeseries(ldf)),
        ("Fig 11  — Stable user envelope",             lambda: fig_stable_envelope(ldf)),
        ("Fig 12  — Resource scatter @ u100",          lambda: fig_scatter_resources(ldf)),
        ("Fig 13  — CPU utilisation evolution",        lambda: fig_cpu_evolution(sdf)),
        ("Fig 14  — RAM pressure (2-vCPU configs)",    lambda: fig_ram_pressure(sdf)),
        ("Fig 15  — MySQL QPS plateau",                lambda: fig_mysql_plateau(sdf, ldf)),
        ("Fig 16  — Resource state at limit",          lambda: fig_resource_at_limit(sdf, ldf)),
    ]

    for desc, fn in steps:
        print(desc)
        try:
            fn()
        except Exception as exc:
            import traceback
            print(f"  ✗  {exc}")
            traceback.print_exc()

    print("\nDone.")


if __name__ == "__main__":
    main()
