#!/usr/bin/env python3
"""
analyse.py — Generate all experiment diagrams from Locust results.

Produces PNG figures in lab/analysis/figures/ covering:
  - Throughput scaling by RAM (2-CPU configs)
  - Throughput scaling by CPU (4-GB configs)
  - Latency scaling (p50 / p95 / p99) for both axes
  - Complete saturation curve for 2cpu-4gb (only fully-completed config)
  - Per-endpoint latency breakdown at reference loads
  - Resource-efficiency metrics (req/s per vCPU, per GB)
  - Summary heatmap of throughput across all configs and user counts

Partial runs (reservation ended mid-experiment) are flagged visually.
"""

from __future__ import annotations
import re
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
import seaborn as sns

# ── paths ────────────────────────────────────────────────────────────────────
LAB_ROOT   = Path(__file__).resolve().parent.parent
RESULTS    = LAB_ROOT / "results"
FIG_DIR    = Path(__file__).resolve().parent / "figures"
FIG_DIR.mkdir(exist_ok=True)

# ── style ────────────────────────────────────────────────────────────────────
STYLE      = "seaborn-v0_8-whitegrid"
FONT_TITLE = 14
FONT_LABEL = 12
FONT_TICK  = 10
DPI        = 180

# Config metadata: label → (vcpus, ram_gb, display_label)
CONFIGS = {
    "2cpu-2gb":  (2, 2,  "2 vCPU / 2 GB"),
    "2cpu-4gb":  (2, 4,  "2 vCPU / 4 GB"),
    "2cpu-8gb":  (2, 8,  "2 vCPU / 8 GB"),
    "1cpu-4gb":  (1, 4,  "1 vCPU / 4 GB"),
    "4cpu-4gb":  (4, 4,  "4 vCPU / 4 GB"),
    "8cpu-4gb":  (8, 4,  "8 vCPU / 4 GB"),
}

# Colour palettes
RAM_PALETTE = {"2cpu-2gb": "#e07b39", "2cpu-4gb": "#2b7bb9", "2cpu-8gb": "#27ae60"}
CPU_PALETTE = {"1cpu-4gb": "#e07b39", "2cpu-4gb": "#2b7bb9",
               "4cpu-4gb": "#9b59b6", "8cpu-4gb": "#27ae60"}

# Points flagged as unreliable: 100% failure rate → VM was already destroyed
# (config, users) pairs excluded from trend lines and capacity calculations
FLAGGED = {
    ("2cpu-8gb",  50),   # 534 total reqs, anomalous rps=36 → reservation cut mid-ramp
    ("2cpu-8gb",  100),  # 100% failure rate – VM destroyed
    ("1cpu-4gb",  100),  # 100% failure rate – VM destroyed
    ("4cpu-4gb",  50),   # 100% failure rate – VM destroyed
    ("8cpu-4gb",  200),  # 100% failure rate – VM destroyed
}

# Configs whose last VALID data point is a lower bound (reservation ended before full sweep)
PARTIAL_CONFIGS = {"2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"}

# ── data loading ─────────────────────────────────────────────────────────────

# New layout:  lab/results/<config>/<run_id>/u<users>_stats.csv
# Old layout:  lab/results/<run_id>_<config>_u<users>_stats.csv  (backward compat)
_NEW_STEP_RE = re.compile(r"^u(\d+)_stats\.csv$")
_OLD_FNAME_RE = re.compile(r"(\d{8}_\d{6})_(\d+cpu-\d+gb)_u(\d+)_stats\.csv$")

def load_all_stats() -> pd.DataFrame:
    """
    Returns a DataFrame with one row per (config, users) pair.
    Supports both the new nested layout and the old flat layout.
    Duplicate run IDs for the same config (e.g. two 2cpu-4gb runs) are averaged.
    """
    rows = []
    seen: set[tuple] = set()

    # New layout: lab/results/<config>/<run_id>/u<N>_stats.csv
    for f in RESULTS.glob("*/*/u*_stats.csv"):
        m = _NEW_STEP_RE.match(f.name)
        if not m:
            continue
        config = f.parent.parent.name
        run_id = f.parent.name
        users  = int(m.group(1))
        if config not in CONFIGS:
            continue
        key = (run_id, config, users)
        if key in seen:
            continue
        seen.add(key)
        df = pd.read_csv(f)
        agg = df[df["Name"] == "Aggregated"].iloc[0]
        rows.append({
            "run_id":       run_id,
            "config":       config,
            "users":        users,
            "rps":          float(agg["Requests/s"]),
            "failures":     int(agg["Failure Count"]),
            "total":        int(agg["Request Count"]),
            "failure_rate": int(agg["Failure Count"]) / int(agg["Request Count"]),
            "p50":          float(agg["50%"]),
            "p75":          float(agg["75%"]),
            "p90":          float(agg["90%"]),
            "p95":          float(agg["95%"]),
            "p99":          float(agg["99%"]),
        })

    # Old flat layout (backward compat): lab/results/<run_id>_<config>_u<N>_stats.csv
    for f in RESULTS.glob("*cpu*_stats.csv"):
        m = _OLD_FNAME_RE.match(f.name)
        if not m:
            continue
        run_id, config, users = m.group(1), m.group(2), int(m.group(3))
        if config not in CONFIGS:
            continue
        key = (run_id, config, users)
        if key in seen:
            continue
        seen.add(key)
        df = pd.read_csv(f)
        agg_row = df[df["Name"] == "Aggregated"].iloc[0]
        rows.append({
            "run_id":       run_id,
            "config":       config,
            "users":        users,
            "rps":          float(agg_row["Requests/s"]),
            "failures":     int(agg_row["Failure Count"]),
            "total":        int(agg_row["Request Count"]),
            "failure_rate": int(agg_row["Failure Count"]) / int(agg_row["Request Count"]),
            "p50":          float(agg_row["50%"]),
            "p75":          float(agg_row["75%"]),
            "p90":          float(agg_row["90%"]),
            "p95":          float(agg_row["95%"]),
            "p99":          float(agg_row["99%"]),
        })

    raw = pd.DataFrame(rows)
    # Average duplicate (config, users) runs
    agg = raw.groupby(["config", "users"], as_index=False).agg(
        rps          = ("rps",          "mean"),
        failures     = ("failures",     "sum"),
        total        = ("total",        "sum"),
        failure_rate = ("failure_rate", "mean"),
        p50          = ("p50",          "mean"),
        p75          = ("p75",          "mean"),
        p90          = ("p90",          "mean"),
        p95          = ("p95",          "mean"),
        p99          = ("p99",          "mean"),
    )
    agg["flagged"] = agg.apply(lambda r: (r.config, r.users) in FLAGGED, axis=1)
    return agg.sort_values(["config", "users"]).reset_index(drop=True)


def load_endpoint_stats(config: str, users: int) -> pd.DataFrame | None:
    """Return per-endpoint stats for a given (config, users) point (first matching run)."""
    # New layout: lab/results/<config>/<run_id>/u<N>_stats.csv
    candidates = sorted((RESULTS / config).glob(f"*/u{users}_stats.csv"))
    # Old flat layout fallback
    if not candidates:
        candidates = sorted(RESULTS.glob(f"*_{config}_u{users}_stats.csv"))
    for f in candidates:
        df = pd.read_csv(f)
        df = df[df["Name"] != "Aggregated"].copy()
        df["endpoint"] = df["Name"].str.replace(r"^(GET|POST|PUT|DELETE)\s+", "", regex=True)
        return df
    return None


def load_history(config: str, users: int) -> pd.DataFrame | None:
    """Return stats_history time-series for a given (config, users) point."""
    # New layout: lab/results/<config>/<run_id>/u<N>_stats_history.csv
    candidates = sorted((RESULTS / config).glob(f"*/u{users}_stats_history.csv"))
    # Old flat layout fallback
    if not candidates:
        candidates = sorted(RESULTS.glob(f"*_{config}_u{users}_stats_history.csv"))
    for f in candidates:
        df = pd.read_csv(f)
        df = df[df["Name"] == "Aggregated"].copy()
        t0 = df["Timestamp"].min()
        df["t_sec"] = df["Timestamp"] - t0
        return df
    return None


# ── helpers ──────────────────────────────────────────────────────────────────

def savefig(name: str, tight: bool = True):
    if tight:
        plt.tight_layout()
    path = FIG_DIR / f"{name}.png"
    plt.savefig(path, dpi=DPI, bbox_inches="tight")
    plt.close()
    print(f"  ✓  {path.relative_to(LAB_ROOT)}")


def valid(df: pd.DataFrame, config: str) -> pd.DataFrame:
    """Return rows for a config that are NOT flagged."""
    return df[(df.config == config) & (~df.flagged)].copy()


def flag_rows(df: pd.DataFrame, config: str) -> pd.DataFrame:
    return df[(df.config == config) & (df.flagged)].copy()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 1 — Throughput vs. Users  (RAM axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_throughput_ram(df: pd.DataFrame):
    configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = RAM_PALETTE[cfg]
            label = CONFIGS[cfg][2]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.rps, "o-", color=color, label=label, lw=2, ms=6)
            fd = flag_rows(df, cfg)
            if not fd.empty:
                ax.plot(fd.users, fd.rps, "x", color=color, ms=10, mew=2,
                        alpha=0.6, zorder=5)

        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("Throughput (req/s)", fontsize=FONT_LABEL)
        ax.set_title("Throughput vs. Concurrent Users — RAM Effect (2 vCPUs)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        cross = mpatches.Patch(facecolor="none", edgecolor="grey",
                               label="✕ = reservation ended / VM crashed")
        ax.legend(title="Configuration", handles=ax.get_legend().legend_handles + [cross],
                  fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        ax.yaxis.set_minor_locator(mticker.AutoMinorLocator())
        savefig("fig1_throughput_ram")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 2 — p95 Latency vs. Users  (RAM axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_latency_ram(df: pd.DataFrame):
    configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = RAM_PALETTE[cfg]
            label = CONFIGS[cfg][2]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.p95, "o-", color=color, label=label, lw=2, ms=6)
            fd = flag_rows(df, cfg)
            if not fd.empty:
                ax.plot(fd.users, fd.p95, "x", color=color, ms=10, mew=2, alpha=0.6)

        ax.axhline(1000, ls="--", color="red", lw=1.2, alpha=0.6, label="1 s SLO")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 3 — Throughput vs. Users  (CPU axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_throughput_cpu(df: pd.DataFrame):
    configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = CPU_PALETTE[cfg]
            label = CONFIGS[cfg][2]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.rps, "o-", color=color, label=label, lw=2, ms=6)
            fd = flag_rows(df, cfg)
            if not fd.empty:
                ax.plot(fd.users, fd.rps, "x", color=color, ms=10, mew=2, alpha=0.6)

        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("Throughput (req/s)", fontsize=FONT_LABEL)
        ax.set_title("Throughput vs. Concurrent Users — CPU Effect (4 GB RAM)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(mticker.ScalarFormatter())
        ax.legend(title="Configuration", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig3_throughput_cpu")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 4 — p95 Latency vs. Users  (CPU axis)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_latency_cpu(df: pd.DataFrame):
    configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(8, 5))
        for cfg in configs:
            color = CPU_PALETTE[cfg]
            label = CONFIGS[cfg][2]
            vd = valid(df, cfg)
            ax.plot(vd.users, vd.p95, "o-", color=color, label=label, lw=2, ms=6)
            fd = flag_rows(df, cfg)
            if not fd.empty:
                ax.plot(fd.users, fd.p95, "x", color=color, ms=10, mew=2, alpha=0.6)

        ax.axhline(1000, ls="--", color="red", lw=1.2, alpha=0.6, label="1 s SLO")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 5 — Complete saturation profile for 2cpu-4gb  (percentile fan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
        # annotate knee
        knee_idx = d["rps"].idxmax()
        ax0 = axes[0]
        ax0.annotate(f"Peak {d.loc[knee_idx,'rps']:.0f} req/s\n@ {d.loc[knee_idx,'users']} users",
                     xy=(d.loc[knee_idx,'users'], d.loc[knee_idx,'rps']),
                     xytext=(d.loc[knee_idx,'users'] * 0.3, d.loc[knee_idx,'rps'] * 0.85),
                     arrowprops=dict(arrowstyle="->", color="grey"),
                     fontsize=FONT_TICK, color="#2b7bb9")

        # bottom — percentile fan
        axes[1].fill_between(d.users, d.p50, d.p95, alpha=0.25, color="#e07b39", label="p50–p95 band")
        axes[1].plot(d.users, d.p50, "o-", color="#27ae60", lw=2, ms=5, label="p50")
        axes[1].plot(d.users, d.p75, "s--", color="#f39c12", lw=1.5, ms=4, label="p75")
        axes[1].plot(d.users, d.p95, "^-", color="#e07b39", lw=2, ms=5, label="p95")
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 6 — Response-time percentile comparison across configs at u50
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_percentile_bars(df: pd.DataFrame):
    target_u = 50
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    pcts  = ["p50", "p75", "p90", "p95", "p99"]
    pct_colors = ["#27ae60", "#f1c40f", "#e67e22", "#e07b39", "#c0392b"]

    sub = df[(df.users == target_u) & (~df.flagged) & df.config.isin(configs_order)].copy()
    sub["display"] = sub.config.map(lambda c: CONFIGS[c][2])
    sub = sub.set_index("config").loc[[c for c in configs_order if c in sub.config.values]]

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(10, 5))
        x = np.arange(len(sub))
        width = 0.14
        for i, (pct, col) in enumerate(zip(pcts, pct_colors)):
            offset = (i - 2) * width
            bars = ax.bar(x + offset, sub[pct].values, width * 0.92,
                          color=col, label=pct, zorder=3)

        ax.set_xticks(x)
        ax.set_xticklabels(sub["display"].tolist() if "display" in sub.columns
                           else [CONFIGS[c][2] for c in sub.index],
                           fontsize=FONT_TICK)
        ax.set_ylabel("Response Time (ms)", fontsize=FONT_LABEL)
        ax.set_title(f"Response-Time Percentiles at {target_u} Concurrent Users",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.legend(title="Percentile", fontsize=FONT_TICK)
        ax.tick_params(labelsize=FONT_TICK)
        savefig("fig6_percentile_bars")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 7 — Endpoint latency breakdown for 2cpu-4gb at u100 and u500
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_endpoint_breakdown(df: pd.DataFrame):
    pairs = [("2cpu-4gb", 100), ("2cpu-4gb", 500)]
    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(14, 5), sharey=False)
        for ax, (cfg, users) in zip(axes, pairs):
            ep = load_endpoint_stats(cfg, users)
            if ep is None:
                ax.text(0.5, 0.5, "No data", ha="center", va="center")
                continue
            ep = ep.sort_values("95%", ascending=False).head(12)
            short_names = []
            for name in ep["endpoint"]:
                n = name.replace("GET ", "").replace("POST ", "").replace("PUT ", "")
                n = n.replace("/api/", "")
                short_names.append(n[:30])
            y = np.arange(len(ep))
            ax.barh(y, ep["95%"].values, color="#2b7bb9", alpha=0.85, label="p95", zorder=3)
            ax.barh(y, ep["50%"].values, color="#27ae60", alpha=0.85, label="p50", zorder=4)
            ax.set_yticks(y)
            ax.set_yticklabels(short_names, fontsize=8)
            ax.set_xlabel("Response Time (ms)", fontsize=FONT_LABEL)
            ax.set_title(f"2 vCPU / 4 GB — {users} users", fontsize=FONT_LABEL,
                         fontweight="bold")
            ax.legend(fontsize=FONT_TICK)
            ax.tick_params(labelsize=FONT_TICK)

        fig.suptitle("Per-Endpoint p50 / p95 Latency (top 12 slowest)",
                     fontsize=FONT_TITLE, fontweight="bold", y=1.01)
        savefig("fig7_endpoint_breakdown")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 8 — Throughput heatmap  (configs × user counts)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_heatmap(df: pd.DataFrame):
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    d = df[~df.flagged].copy()
    d = d[d.config.isin(configs_order)]
    pivot = d.pivot_table(index="config", columns="users", values="rps", aggfunc="mean")
    pivot = pivot.reindex(configs_order)
    row_labels = [CONFIGS[c][2] for c in pivot.index]

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(12, 4))
        mask = pivot.isna()
        g = sns.heatmap(
            pivot, ax=ax, mask=mask,
            cmap="YlOrRd", annot=True, fmt=".0f",
            linewidths=0.5, linecolor="white",
            cbar_kws={"label": "Throughput (req/s)", "shrink": 0.8},
        )
        ax.set_yticklabels(row_labels, rotation=0, fontsize=FONT_TICK)
        ax.set_xticklabels([str(c) for c in pivot.columns], fontsize=FONT_TICK)
        ax.set_xlabel("Concurrent Users", fontsize=FONT_LABEL)
        ax.set_ylabel("VM Configuration", fontsize=FONT_LABEL)
        ax.set_title("Throughput Heatmap — req/s by Configuration and Load Level\n"
                     "(grey = not tested / reservation ended)",
                     fontsize=FONT_TITLE, fontweight="bold")
        savefig("fig8_heatmap")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 9 — Resource efficiency  (req/s per vCPU and per GB at u100)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_efficiency(df: pd.DataFrame):
    """
    Show stable-user capacity per provisioned resource unit:
      - stable users per vCPU  (CPU axis: 1/2/4/8 vCPU @ 4 GB)
      - stable users per GB    (RAM axis: 2/4/8 GB @ 2 vCPU)
    Derived from the stable envelope (max users with p95 < 1000 ms, 0% failures).
    Partial configs (hatched) show lower bounds.
    """
    SLO_P95 = 1000
    cpu_configs = ["1cpu-4gb", "2cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    ram_configs = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb"]

    def stable_users(config):
        sub = df[(df.config == config) & (~df.flagged) &
                 (df.failure_rate == 0) & (df.p95 < SLO_P95)]
        return int(sub["users"].max()) if not sub.empty else 0

    rows = []
    for cfg in set(cpu_configs + ram_configs):
        rows.append({
            "config":  cfg,
            "label":   CONFIGS[cfg][2],
            "vcpus":   CONFIGS[cfg][0],
            "ram":     CONFIGS[cfg][1],
            "stable":  stable_users(cfg),
            "partial": cfg in PARTIAL_CONFIGS,
        })
    edf = pd.DataFrame(rows)

    with plt.style.context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(13, 5))

        def _bars(ax, sub, x_col, y_col, color_fn, xlabel, ylabel, title):
            sub = sub.sort_values(x_col).copy()
            sub["y"] = sub["stable"] / sub[y_col]
            labels = sub["label"].tolist()
            for i, (_, row) in enumerate(sub.iterrows()):
                hatch = "//" if row["partial"] else ""
                ax.bar(i, row["y"], color=color_fn(row["config"]),
                       hatch=hatch, edgecolor="white", zorder=3)
                prefix = "≥" if row["partial"] else ""
                ax.text(i, row["y"] + 1, f"{prefix}{row['y']:.0f}",
                        ha="center", va="bottom", fontsize=FONT_TICK, fontweight="bold")
            ax.set_xticks(range(len(sub)))
            ax.set_xticklabels(labels, rotation=15, fontsize=FONT_TICK)
            ax.set_ylabel(ylabel, fontsize=FONT_LABEL)
            ax.set_title(title, fontsize=FONT_LABEL, fontweight="bold")
            ax.tick_params(axis="y", labelsize=FONT_TICK)
            ax.set_ylim(0, sub["y"].max() * 1.3)

        _bars(axes[0],
              edf[edf.config.isin(cpu_configs)], "vcpus", "vcpus",
              lambda c: CPU_PALETTE.get(c, "#888"),
              "vCPUs", "Stable Users per vCPU",
              "CPU Efficiency\n(stable users / vCPU @ 4 GB RAM)")

        _bars(axes[1],
              edf[edf.config.isin(ram_configs)], "ram", "ram",
              lambda c: RAM_PALETTE.get(c, "#888"),
              "RAM (GB)", "Stable Users per GB RAM",
              "RAM Efficiency\n(stable users / GB @ 2 vCPUs)")

        solid_patch = mpatches.Patch(facecolor="grey", label="Fully tested")
        hatch_patch = mpatches.Patch(facecolor="grey", hatch="//", edgecolor="white",
                                     label="Partial — lower bound")
        for ax in axes:
            ax.legend(handles=[solid_patch, hatch_patch], fontsize=8)

        fig.suptitle("Resource Provisioning Efficiency\n"
                     "(max stable concurrent users per provisioned resource unit)",
                     fontsize=FONT_TITLE, fontweight="bold")
        savefig("fig9_efficiency")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 10 — Time-series response time for 2cpu-4gb at u100 and u1000
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_timeseries(df: pd.DataFrame):
    pairs = [("2cpu-4gb", 100, "#2b7bb9"), ("2cpu-4gb", 1000, "#c0392b")]
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 11 — Stable user envelope: max users before latency SLO breached
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_stable_envelope(df: pd.DataFrame):
    """
    For each config, find the maximum user count where p95 < 1000 ms
    and failure_rate == 0 and the data point is NOT flagged.
    This is the "stable user envelope" — the IaC provisioning outcome.
    """
    SLO_P95 = 1000   # ms
    configs_order = ["2cpu-2gb", "2cpu-4gb", "2cpu-8gb", "1cpu-4gb", "4cpu-4gb", "8cpu-4gb"]
    data = []
    for cfg in configs_order:
        sub = df[(df.config == cfg) & (~df.flagged) & (df.failure_rate == 0) &
                 (df.p95 < SLO_P95)].copy()
        max_u = int(sub["users"].max()) if not sub.empty else 0
        data.append({"config": cfg, "label": CONFIGS[cfg][2], "max_stable_users": max_u,
                     "vcpus": CONFIGS[cfg][0], "ram": CONFIGS[cfg][1]})

    edf = pd.DataFrame(data)

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(9, 5))
        colors = [RAM_PALETTE.get(c, CPU_PALETTE.get(c, "#888")) for c in edf.config]

        for idx, (_, row) in enumerate(edf.iterrows()):
            is_partial = row["config"] in PARTIAL_CONFIGS
            hatch = "//" if is_partial else ""
            bar = ax.bar(row["label"], row["max_stable_users"],
                         color=colors[idx], hatch=hatch, edgecolor="white",
                         linewidth=0.8, zorder=3)
            label_text = f"≥{row['max_stable_users']}" if is_partial else str(row["max_stable_users"])
            ax.text(idx, row["max_stable_users"] + 5, label_text,
                    ha="center", va="bottom", fontsize=FONT_TICK + 1, fontweight="bold")

        ax.set_ylabel("Max Stable Users", fontsize=FONT_LABEL)
        ax.set_title(f"Stable Capacity Envelope per VM Configuration\n"
                     f"(max users with p95 < {SLO_P95} ms and 0% failures)",
                     fontsize=FONT_TITLE, fontweight="bold")
        ax.tick_params(axis="x", rotation=18, labelsize=FONT_TICK)
        ax.tick_params(axis="y", labelsize=FONT_TICK)
        ax.set_xticks(range(len(edf)))
        ax.set_xticklabels(edf["label"].tolist(), rotation=18, fontsize=FONT_TICK)
        ax.set_ylim(0, edf["max_stable_users"].max() * 1.22)

        solid_patch  = mpatches.Patch(facecolor="grey", label="Fully tested")
        hatch_patch  = mpatches.Patch(facecolor="grey", hatch="//", edgecolor="white",
                                      label="Partial (reservation ended) — value is a lower bound")
        ax.legend(handles=[solid_patch, hatch_patch], fontsize=FONT_TICK, loc="upper right")
        savefig("fig11_stable_envelope")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FIGURE 12 — RAM vs. CPU 2-D scatter: throughput @ u100
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fig_scatter_resources(df: pd.DataFrame):
    target_u = 100
    sub = df[(df.users == target_u) & (~df.flagged)].copy()
    sub = sub.drop_duplicates(subset="config")
    sub["vcpus"] = sub.config.map(lambda c: CONFIGS[c][0])
    sub["ram"]   = sub.config.map(lambda c: CONFIGS[c][1])
    sub["label"] = sub.config.map(lambda c: CONFIGS[c][2])

    with plt.style.context(STYLE):
        fig, ax = plt.subplots(figsize=(7, 5))
        sc = ax.scatter(sub["vcpus"], sub["ram"], s=sub["rps"] * 4,
                        c=sub["rps"], cmap="YlOrRd", zorder=4,
                        edgecolors="grey", linewidths=0.6)
        for _, row in sub.iterrows():
            ax.annotate(f"{row['rps']:.0f} r/s",
                        (row.vcpus, row.ram),
                        textcoords="offset points", xytext=(8, 4),
                        fontsize=8)
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print("Loading Locust results …")
    df = load_all_stats()
    print(f"  {len(df)} (config, users) data points across "
          f"{df.config.nunique()} configurations\n")
    print(f"Configs present: {sorted(df.config.unique())}")
    print(f"User counts    : {sorted(df.users.unique())}\n")
    print(f"Output → {FIG_DIR}\n")

    steps = [
        ("Fig 1  — Throughput RAM effect",       fig_throughput_ram),
        ("Fig 2  — Latency RAM effect",          fig_latency_ram),
        ("Fig 3  — Throughput CPU effect",       fig_throughput_cpu),
        ("Fig 4  — Latency CPU effect",          fig_latency_cpu),
        ("Fig 5  — Saturation profile 2cpu-4gb", fig_saturation_2cpu4gb),
        ("Fig 6  — Percentile bars @ u50",       fig_percentile_bars),
        ("Fig 7  — Endpoint breakdown",          fig_endpoint_breakdown),
        ("Fig 8  — Throughput heatmap",          fig_heatmap),
        ("Fig 9  — Resource efficiency",         fig_efficiency),
        ("Fig 10 — Time-series",                 fig_timeseries),
        ("Fig 11 — Stable user envelope",        fig_stable_envelope),
        ("Fig 12 — Resource scatter",            fig_scatter_resources),
    ]

    for desc, fn in steps:
        print(desc)
        try:
            fn(df)
        except Exception as exc:
            print(f"  ✗  {exc}")

    print("\nDone.")


if __name__ == "__main__":
    main()
