# CorrectExam — Performance Lab

Step-by-step workspace for validating IaC configuration impact on application performance.
Each step is self-contained: run it, verify it works, then move on.

---

## Goal

Run the same application under the same load with different infrastructure configs
and observe which config parameters produce measurable differences in performance metrics.

Local first → then Grid5000 (multiple VMs with different configs).

---

## Repository Layout (relevant to this lab)

```
lab/
├── README.md          ← this file — master reference
├── METRICS.md         ← what to monitor, why, and how (read this before Grafana)
└── scenarios/         ← locust files, one per scenario (added step by step)

workload/
├── seed.py            ← imports testData bundles into a running stack
└── locust/            ← previous locust files (kept for reference only)

testData/
├── 136.json           ← 127 MB — 150 students, 139 sheets, 1946 responses (primary seed)
├── 357.json           ← 81 MB  — 17 students,  17 sheets,  255 responses  (quick smoke test)
└── 265.json           ← 1 GB   — large dataset, avoid on low RAM

docker-compose.yml     ← full local stack
monitoring/prometheus/ ← Prometheus config (scrape targets)
monitoring/grafana/    ← Grafana provisioning (datasource + dashboards)
```

---

## Local Stack Quick Reference

```bash
# Start everything fresh (wipes all volumes)
docker-compose down -v && docker-compose up -d

# Start without wiping data
docker-compose up -d

# Seed the DB from testData (run once after a fresh start)
python workload/seed.py --files 136

# Stop everything
docker-compose down
```

| Service      | URL                              | Notes                          |
|--------------|----------------------------------|--------------------------------|
| App frontend | http://localhost:8080            | Angular UI                     |
| App API      | http://localhost:8082            | Quarkus REST API               |
| App metrics  | http://localhost:9091/management/prometheus | Micrometer/Prometheus  |
| Grafana      | http://localhost:3000            | admin / admin                  |
| Prometheus   | http://localhost:9092            |                                |
| MySQL        | localhost:3306                   | gradescope / test              |
| Minio        | http://localhost:9090            | admin / minioadmin             |

---

## Progress Log

### Step 1 — Baseline local run ✅ (completed 2026-06-25)
- Wiped old DB (had 100 admin-owned records from previous junk locust runs)
- Added `mysqld-exporter v0.14.0` to docker-compose for DB-level metrics (v0.15.1 dropped DATA_SOURCE_NAME support)
- Added `QUARKUS_HTTP_LIMITS_MAX_BODY_SIZE=256M` to docker-compose for large imports
- Seeded DB 3× with `testData/357.json` + 1× `testData/136.json` (course-only, no students)
- Final DB state: 4 courses, 4 exams, 51 exam sheets, ~765 student responses
- **Root cause of previous non-visible metrics:** locust ran as `user/user`
  but all data was owned by `admin` → browse endpoints returned empty arrays
  → zero DB pressure → nothing to see in Grafana

**Known issue — `testData/136.json` full import fails with silent 500:**
`importCourse` catches all exceptions without logging. The failure is in the student data
path (1946 responses + 139 sheets). `importCourseWithoutStudentData` works fine for 136.json.
Root cause is unconfirmed (likely a constraint violation deep in the relationship-building loop).
**Impact:** We use 357.json ×3 for seeding instead. Dataset is sufficient for step 2.

### Step 2 — First scenario: browse workload ✅ (completed 2026-06-25)
- Wrote `lab/scenarios/s01_browse.py` — read-only browse, 8 tasks, uses `admin` credentials
- Smoke test: 10 users × 30s → **0 failures**, 154 requests, 5.27 req/s, p50=29ms, p95=180ms
- Next: run at 20-50 users and confirm Grafana panels show visible activity

### Step 3 — Grading workload (DB write-heavy) ✅ (completed 2026-06-26)
- Wrote `lab/scenarios/s02_grade.py` — realistic teacher grading flow, no hardcoded IDs
- Tasks: grade_response (PUT, weight 4), view_sheet (GET, weight 4), check_progress (GET, weight 2), switch_question (GET, weight 1)
- Smoke test: 10 users × 30s → **0 failures**, PUT p50=28ms, PUT p95=56ms
- Run at 20-100 users and observe: hikaricp_connections_pending, innodb_row_lock_waits, com_update rate

### Step 4 — Combined workload ✅ (completed 2026-06-26)
- Wrote `lab/scenarios/s03_mixed.py` — BrowseUser (weight 3, 60%) + GraderUser (weight 2, 40%)
- Shared `_BaseUser` base class, no code duplication between user types
- Smoke test: 20 users × 30s → **0 failures**, PUT p50=19ms, p95=55ms, overall p50=12ms
- **This is the reference workload for Grid5000 experiments**
- Results land in `lab/results/` as CSV + HTML (see Running section below)

### Step 5 — Grid5000 deployment
See `grid5000/README.md` for the full workflow. Summary:

- [ ] `python3 grid5000/reserve.py --site nancy --walltime 03:00:00` (from laptop)
- [ ] SSH to node, clone repo, run `bash grid5000/setup_node.sh`
- [ ] Open SSH tunnel for Grafana: `ssh -N -L 3000:localhost:3000 -L 9092:localhost:9092 <node>`
- [ ] `python3 grid5000/scripts/run_experiment.py --phase 1` (Phase 1: RAM sweep)
- [ ] `python3 grid5000/scripts/run_experiment.py --phase 2` (Phase 2: CPU sweep)
- [ ] `scp -r <node>:/root/CorrigeExam/lab/results/ lab/grid5000_results/`

Key infra files:
- `grid5000/reserve.py`          — EnOSlib node reservation
- `grid5000/experiments.yml`     — 8 VM configs (2 phases)
- `grid5000/terraform/`          — libvirt VM definition (parameterized RAM/vCPU)
- `grid5000/vm-docker-compose.yml` — app stack for VMs (no Prometheus/Grafana)
- `grid5000/monitoring/`         — Prometheus on bare-metal node, file-SD targets

---

## Running Locust — how to save results

Always use `--csv` and `--html` so results are preserved for analysis.

```bash
# Naming convention: <timestamp>_<scenario>_<config-label>
RUN_ID=$(date +%Y%m%d_%H%M%S)

# Local machine
locust -f lab/scenarios/s03_mixed.py \
       --host http://localhost:8082 \
       --headless -u 50 -r 5 --run-time 5m \
       --csv  lab/results/${RUN_ID}_local \
       --html lab/results/${RUN_ID}_local.html

# Grid5000 VM (replace <vm-ip> and config label)
locust -f lab/scenarios/s03_mixed.py \
       --host http://<vm-ip>:8082 \
       --headless -u 50 -r 5 --run-time 5m \
       --csv  lab/results/${RUN_ID}_vm1_2vcpu_4g \
       --html lab/results/${RUN_ID}_vm1_2vcpu_4g.html

# Web UI only (interactive, no auto-save — for exploration)
locust -f lab/scenarios/s03_mixed.py --host http://localhost:8082
```

### Result files per run

| File | What's in it |
|------|-------------|
| `*_stats.csv` | Per-endpoint totals: req count, avg, p50/p95/p99, failures |
| `*_stats_history.csv` | Same metrics every ~10s — **use this for time-series plots** |
| `*_failures.csv` | Details of any failed requests |
| `*.html` | Standalone shareable HTML report (open in browser, send as file) |

### Recommended starting user counts

| Scenario | Local (18GB/12CPU) | Grid5000 VM (2vCPU/4GB) |
|----------|--------------------|------------------------|
| s01_browse | 100–500 | 20–100 |
| s02_grade  | 50–200  | 10–50  |
| s03_mixed  | 50–200  | 20–100 |

Rule: start at 20, confirm 0 failures, double until latency climbs.

---

## DB Reset

```bash
# Wipe and reseed (takes ~3-5 min for 136.json)
docker-compose down -v
docker-compose up -d
# wait ~60s for MySQL to be healthy, then:
python workload/seed.py --files 136
```

---

## Key Decisions Log

| Date       | Decision                                   | Reason                                                  |
|------------|--------------------------------------------|---------------------------------------------------------|
| 2026-06-25 | Use `admin` credentials in locust scenarios | `user` can only see their own data; admin sees everything |
| 2026-06-25 | Seed with `136.json` not `357.json`        | 357 only has 17 students — not enough volume for stress  |
| 2026-06-25 | Added `mysqld-exporter` to docker-compose  | DB metrics needed to identify InnoDB/query bottlenecks   |
