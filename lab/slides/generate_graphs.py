#!/usr/bin/env python3
"""
Generate slide-ready performance graphs from run 20260626_015650_local.
Output: lab/slides/*.png  (5 graphs, ~200 dpi, clean white background)
"""

import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib.gridspec import GridSpec

# ── paths ──────────────────────────────────────────────────────────────────────
BASE  = "/Users/helhayan/Desktop/Mathese/expirements/CorrigeExam"
RUN   = "20260626_015650_local"
RDIR  = f"{BASE}/lab/results"
OUT   = f"{BASE}/lab/slides"
os.makedirs(OUT, exist_ok=True)

# ── palette ────────────────────────────────────────────────────────────────────
C = {
    "blue":   "#2563EB",
    "green":  "#16A34A",
    "orange": "#EA580C",
    "red":    "#DC2626",
    "purple": "#7C3AED",
    "gray":   "#6B7280",
    "lblue":  "#BFDBFE",
    "lgreen": "#BBF7D0",
    "lorange":"#FED7AA",
}

# ── global style ───────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family":        "DejaVu Sans",
    "font.size":          12,
    "axes.titlesize":     14,
    "axes.titleweight":   "bold",
    "axes.labelsize":     12,
    "xtick.labelsize":    10,
    "ytick.labelsize":    10,
    "axes.spines.top":    False,
    "axes.spines.right":  False,
    "figure.facecolor":   "white",
    "axes.facecolor":     "white",
    "savefig.dpi":        200,
    "savefig.bbox":       "tight",
    "savefig.facecolor":  "white",
    "axes.grid":          True,
    "grid.alpha":         0.3,
    "grid.linestyle":     "--",
    "grid.color":         "#9CA3AF",
})

# ── load data ──────────────────────────────────────────────────────────────────
stats = pd.read_csv(f"{RDIR}/{RUN}_stats.csv")
hist  = pd.read_csv(f"{RDIR}/{RUN}_stats_history.csv")

# normalise time
t0           = hist["Timestamp"].min()
hist["elapsed"] = hist["Timestamp"] - t0

# history is all Aggregated rows (Name == "")
hist_agg = hist[hist["Name"].isin(["", "Aggregated"])].copy()

# per-endpoint stats (drop the Aggregated summary row)
ep = stats[stats["Name"] != "Aggregated"].copy()

# ── short display labels ───────────────────────────────────────────────────────
def short(name: str) -> str:
    name = name.replace("GET /api/", "").replace("POST /api/", "POST ").replace("PUT /api/", "PUT ")
    # strip parenthetical qualifiers except (grade) which matters
    import re
    name = re.sub(r"\s*\((?!grade)[^)]+\)", "", name)
    return name.strip()

ep["label"] = ep["Name"].apply(short)


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 1 — Throughput + Active users over time
# ══════════════════════════════════════════════════════════════════════════════
fig, ax1 = plt.subplots(figsize=(11, 5))
ax2 = ax1.twinx()

ax1.fill_between(hist_agg["elapsed"], hist_agg["Requests/s"],
                 alpha=0.18, color=C["blue"])
ax1.plot(hist_agg["elapsed"], hist_agg["Requests/s"],
         color=C["blue"], linewidth=2.5, label="Requests / s")

ax2.plot(hist_agg["elapsed"], hist_agg["User Count"],
         color=C["orange"], linewidth=2, linestyle="--", label="Active users")
ax2.set_ylim(0, 65)
ax2.set_ylabel("Active users", color=C["orange"], fontsize=12)
ax2.tick_params(axis="y", labelcolor=C["orange"])

ax1.set_xlabel("Elapsed time (s)")
ax1.set_ylabel("Requests / second", color=C["blue"], fontsize=12)
ax1.tick_params(axis="y", labelcolor=C["blue"])
ax1.set_title("Throughput over time  —  s03_mixed, 50 users (5/s ramp)", pad=14)

# annotate steady-state throughput
steady = hist_agg[hist_agg["User Count"] == 50]["Requests/s"]
ax1.axhline(steady.mean(), color=C["blue"], linestyle=":", linewidth=1.4, alpha=0.7)
ax1.text(hist_agg["elapsed"].max() * 0.72, steady.mean() + 0.8,
         f"avg {steady.mean():.1f} req/s", color=C["blue"], fontsize=10)

# combined legend
h1, l1 = ax1.get_legend_handles_labels()
h2, l2 = ax2.get_legend_handles_labels()
ax1.legend(h1 + h2, l1 + l2, loc="lower right", framealpha=0.9)

fig.tight_layout()
fig.savefig(f"{OUT}/01_throughput_over_time.png")
plt.close(fig)
print("✓ 01_throughput_over_time.png")


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2 — Latency percentiles over time  (p50 / p90 / p95)
# ══════════════════════════════════════════════════════════════════════════════
fig, ax = plt.subplots(figsize=(11, 5))

ax.plot(hist_agg["elapsed"], hist_agg["50%"],  color=C["blue"],   lw=2.5, label="p50 (median)")
ax.plot(hist_agg["elapsed"], hist_agg["90%"],  color=C["orange"], lw=2.5, label="p90")
ax.plot(hist_agg["elapsed"], hist_agg["95%"],  color=C["red"],    lw=2,   label="p95", linestyle="--")

# shade ramp-up period
ramp_end = hist_agg[hist_agg["User Count"] == 50]["elapsed"].min()
ax.axvspan(0, ramp_end, alpha=0.07, color=C["orange"], label="ramp-up")
ax.axvline(ramp_end, color=C["orange"], linewidth=1, linestyle=":", alpha=0.6)
ax.text(ramp_end + 2, ax.get_ylim()[1] * 0.9, "steady\nstate →",
        fontsize=9, color=C["gray"])

ax.set_xlabel("Elapsed time (s)")
ax.set_ylabel("Response time (ms)")
ax.set_title("Latency percentiles over time  —  all endpoints aggregated", pad=14)
ax.legend(loc="upper right", framealpha=0.9)

fig.tight_layout()
fig.savefig(f"{OUT}/02_latency_over_time.png")
plt.close(fig)
print("✓ 02_latency_over_time.png")


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 3 — Latency by endpoint (p50 / p90 / p95) — horizontal bars
# ══════════════════════════════════════════════════════════════════════════════
# keep only "primary" rows (drop setup/on-demand duplicates but keep (grade) and (progress))
import re
primary = ep[~ep["Name"].str.contains(r"\(setup\)|\(on.demand\)", regex=True)].copy()
primary = primary.sort_values("95%", ascending=True)

fig, ax = plt.subplots(figsize=(11, 7))
y  = np.arange(len(primary))
bh = 0.26

b1 = ax.barh(y + bh,  primary["50%"], bh, color=C["blue"],   label="p50")
b2 = ax.barh(y,       primary["90%"], bh, color=C["orange"], label="p90")
b3 = ax.barh(y - bh,  primary["95%"], bh, color=C["red"],    label="p95", alpha=0.9)

# colour PUT bars differently
for i, (_, row) in enumerate(primary.iterrows()):
    if row["Type"] == "PUT":
        for bars, col in [(b1, C["blue"]), (b2, C["orange"]), (b3, C["red"])]:
            idx = list(primary.index).index(_)  # position in sorted df
            bars[idx].set_edgecolor("black")
            bars[idx].set_linewidth(1.4)

ax.set_yticks(y)
ax.set_yticklabels(primary["label"], fontsize=9.5)
ax.set_xlabel("Response time (ms)")
ax.set_title("Latency per endpoint  —  p50 / p90 / p95\n(bold border = write operation)", pad=14)
ax.legend(loc="lower right", framealpha=0.9)
ax.xaxis.set_minor_locator(mticker.AutoMinorLocator())

fig.tight_layout()
fig.savefig(f"{OUT}/03_latency_by_endpoint.png")
plt.close(fig)
print("✓ 03_latency_by_endpoint.png")


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 4 — Request volume by endpoint (horizontal bar, colour by type)
# ══════════════════════════════════════════════════════════════════════════════
vol = ep[~ep["Name"].str.contains(r"\(setup\)|\(on.demand\)", regex=True)].copy()
vol = vol.sort_values("Request Count", ascending=True)

bar_colors = [C["orange"] if t == "PUT" else (C["red"] if t == "POST" else C["blue"])
              for t in vol["Type"]]

fig, ax = plt.subplots(figsize=(11, 6))
bars = ax.barh(vol["label"], vol["Request Count"], color=bar_colors)

# value labels
for bar, v in zip(bars, vol["Request Count"]):
    ax.text(bar.get_width() + 8, bar.get_y() + bar.get_height() / 2,
            f"{v:,}", va="center", fontsize=9)

legend_handles = [
    mpatches.Patch(color=C["blue"],   label="GET  (read)"),
    mpatches.Patch(color=C["orange"], label="PUT  (write)"),
    mpatches.Patch(color=C["red"],    label="POST (auth)"),
]
ax.legend(handles=legend_handles, loc="lower right", framealpha=0.9)
ax.set_xlabel("Total requests during run")
ax.set_title("Request volume by endpoint  —  5-minute run, 50 users", pad=14)
ax.set_xlim(0, vol["Request Count"].max() * 1.18)

fig.tight_layout()
fig.savefig(f"{OUT}/04_request_volume.png")
plt.close(fig)
print("✓ 04_request_volume.png")


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 5 — Read / Write / Auth latency profile comparison
# ══════════════════════════════════════════════════════════════════════════════
pcts    = ["50%", "66%", "75%", "90%", "95%", "99%"]
pct_lbl = ["p50", "p66", "p75", "p90", "p95", "p99"]
x = np.arange(len(pcts))
bw = 0.28

reads  = ep[ep["Type"] == "GET"][pcts].mean()
writes = ep[ep["Type"] == "PUT"][pcts].mean()
auth   = ep[ep["Name"].str.contains("authenticate")][pcts]
auth_v = auth.values[0] if not auth.empty else np.zeros(len(pcts))

fig, ax = plt.subplots(figsize=(10, 5))
ax.bar(x - bw,   reads.values,  bw, color=C["blue"],   label="GET reads (avg)")
ax.bar(x,        writes.values, bw, color=C["orange"],  label="PUT writes (grade)")
ax.bar(x + bw,   auth_v,        bw, color=C["red"],     label="POST /authenticate")

ax.set_xticks(x)
ax.set_xticklabels(pct_lbl)
ax.set_ylabel("Response time (ms)")
ax.set_title("Latency profile by operation class  —  read / write / auth", pad=14)
ax.legend(framealpha=0.9)

# annotate auth p50 spike
auth_p50_idx = list(pcts).index("50%")
ax.annotate("100 ms\n(DB lookup\non every login)",
            xy=(x[auth_p50_idx] + bw, auth_v[auth_p50_idx]),
            xytext=(x[auth_p50_idx] + bw + 0.6, auth_v[auth_p50_idx] + 20),
            arrowprops=dict(arrowstyle="->", color=C["gray"]),
            fontsize=9, color=C["gray"])

fig.tight_layout()
fig.savefig(f"{OUT}/05_read_write_auth_comparison.png")
plt.close(fig)
print("✓ 05_read_write_auth_comparison.png")


# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 6 — Summary tile (key KPIs) — good opener/closer slide
# ══════════════════════════════════════════════════════════════════════════════
agg = stats[stats["Name"] == "Aggregated"].iloc[0]
steady_rps = hist_agg[hist_agg["User Count"] == 50]["Requests/s"].mean()

kpis = [
    ("Total requests",        f"{int(agg['Request Count']):,}",    C["blue"]),
    ("Failure rate",          "0 %",                                C["green"]),
    ("Avg throughput",        f"{steady_rps:.1f} req/s",           C["blue"]),
    ("Median latency (p50)",  f"{int(agg['50%'])} ms",             C["green"]),
    ("p95 latency",           f"{int(agg['95%'])} ms",             C["orange"]),
    ("Auth p50",              f"{int(ep[ep['Name'].str.contains('authenticate')]['50%'].values[0])} ms",
                                                                    C["red"]),
]

fig = plt.figure(figsize=(12, 3.5))
gs  = GridSpec(1, len(kpis), figure=fig, wspace=0.05)

for i, (label, value, color) in enumerate(kpis):
    ax = fig.add_subplot(gs[i])
    ax.set_facecolor(color + "18")          # very light tint
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(color)
    ax.spines["left"].set_linewidth(4)
    ax.spines["bottom"].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])
    ax.text(0.5, 0.62, value, transform=ax.transAxes,
            ha="center", va="center", fontsize=20, fontweight="bold", color=color)
    ax.text(0.5, 0.18, label, transform=ax.transAxes,
            ha="center", va="center", fontsize=10, color=C["gray"], wrap=True)

fig.suptitle("Run summary  —  s03_mixed · 50 users · 5 min · local VM",
             fontsize=13, fontweight="bold", y=1.02)
fig.tight_layout()
fig.savefig(f"{OUT}/00_summary_kpis.png", bbox_inches="tight")
plt.close(fig)
print("✓ 00_summary_kpis.png")

print(f"\nAll graphs saved to {OUT}/")
