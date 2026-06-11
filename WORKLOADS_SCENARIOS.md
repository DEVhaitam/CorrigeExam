# Workloads & Scenarios

Reference for the experiment matrix. Covers the four workload profiles
(W1–W4), the eight scenarios (S0–S8), what each does to the system, and
how to use the results.

---

## Architecture of a run

```
run.py
  └─ run_scenario.sh <scenario> up   → docker-compose up (baseline + override + .env)
  └─ warmup 30s
  └─ locust (headless) → HTTP traffic → Quarkus :8082
  └─ cooldown 15s
  └─ snapshot.py
       ├─ PromQL query_range → prom_snapshot.json
       ├─ docker exec → gc.log
       ├─ docker exec → mysql_status.txt
       └─ docker stats → docker_stats.txt
  └─ run_scenario.sh <scenario> down
```

One **cell** = (scenario × workload × intensity). Results land in
`results/<run_id>/`. Each `run_id` is a UUID; `metadata.yaml` inside ties
it back to the cell coordinates.

---

## Observability stack

| Service | URL | What to watch |
|---|---|---|
| Grafana | http://localhost:3000 (admin/admin) | JVM Micrometer dashboard |
| Prometheus | http://localhost:9092 | raw metrics, `/targets` to check scrapes |
| Quarkus metrics | http://localhost:8082/q/metrics | live JVM + HTTP metrics |
| mysqld_exporter | http://localhost:9104/metrics | raw MySQL counters |
| MinIO | http://localhost:9090 (admin/minioadmin) | storage backend |

> **macOS caveat.** `node-exporter` sees the Docker Desktop Linux VM,
> not the host. `node_cpu_*` and `node_memory_*` are unreliable.
> Everything else (cAdvisor, Micrometer, mysqld_exporter) is accurate.

---

## Workloads

### W1 — browse (light read-only)

**Purpose.** Control workload. Simulates teachers browsing courses and
exams with no heavy I/O.

**Tasks (weights):**

| Weight | Endpoint | What it does |
|---|---|---|
| 5 | `GET /api/courses?page=0&size=20` | course list |
| 4 | `GET /api/exams?page=0&size=20` | exam list, caches IDs for detail calls |
| 3 | `GET /api/students?page=0&size=50` | student list |
| 2 | `GET /api/exams/{id}` | exam detail (id from prior list) |
| 1 | `GET /management/health` | cheapest possible ping |

**Wait time.** `between(1, 3)` seconds — leisurely browser pace.

**Intensity axis.**

| Level | Users | Ramp |
|---|---|---|
| low | 5 | 1/s |
| medium | 20 | 2/s |
| high | 50 | 5/s |

**Dominant stress.** JVM CPU (JSON serialization), MySQL read path,
query result cache. Small working set → pages stay in buffer pool.

**Used in.** E3-cpu-cap (as part of W4-mixed's 60% browse share).

---

### W2 — upload (heap + I/O dominant)

**Purpose.** Stress JVM heap by forcing large PDF bytes through the
JVM before they stream to MinIO. Headline workload for L1 and L4-memory
scenarios.

**Tasks (weights):**

| Weight | Endpoint | What it does |
|---|---|---|
| 6 | `POST /api/scans` (JSON) + `POST /api/uploadScan/{scanId}` (multipart) | create scan record then upload PDF |
| 1 | `GET /api/exam-sheets?examId={id}` | verify scan list (low-cost read) |

The upload is a **two-step flow**:
1. `POST /api/scans` with `{"name": "locust-scan-xxx"}` → returns `{id}`.
2. `POST /api/uploadScan/{id}` with multipart field `"file"` = PDF bytes.

Step 2 hits `ScanService.uploadFile` → streams bytes to MinIO.
The JVM buffers the raw multipart bytes in heap before writing.

**Synthetic PDF pool** (`workload/data/synthetic-exams/`):

| File | Size | Share |
|---|---|---|
| exam-200k.pdf | 200 KB | 70% |
| exam-2m.pdf | 2 MB | 25% |
| exam-10m.pdf | 10 MB | 5% |

Generate once: `python3 workload/data/generate_pdfs.py`

**Wait time.** `between(2, 5)` seconds — uploads aren't bursty.

**Intensity axis.**

| Level | Users | Ramp |
|---|---|---|
| low | 3 | 1/s |
| medium | 10 | 2/s |
| high | 25 | 5/s |

Lower user counts than W1/W3 because each request is much heavier (~2 MB
mean payload vs ~2 KB for reads).

**Used in.** E1-jvm-heap, E4-mem-cap.

---

### W3 — grade (DB write-heavy)

**Purpose.** Saturate MySQL write path and expose lock contention on
shared exam rows. Headline workload for all L3 (MySQL / Quarkus pool)
scenarios.

**Tasks (weights):**

| Weight | Endpoint | What it does |
|---|---|---|
| 8 | `POST /api/student-responses` | **hot path** — save a grade (write + lock) |
| 2 | `GET /api/exam-sheets?examId={id}` | fetch sheets to grade |
| 1 | `GET /api/questions?examId={id}` | fetch question structure |
| 1 | `GET /api/exams/{id}/results` | aggregation read (heaviest query) |

Setup in `on_start`: fetch an existing exam ID, its sheet IDs, and
question IDs so grade saves use real foreign keys.

**Wait time.** `between(0.5, 2)` seconds — graders click fast.

**Intensity axis.**

| Level | Users | Ramp |
|---|---|---|
| low | 5 | 1/s |
| medium | 20 | 2/s |
| high | 50 | 5/s |

Lock contention and connection exhaustion only emerge at scale.
The interesting effects appear at medium/high.

**Used in.** E2-mysql-buffer, E2b-cross-layer-trap.

---

### W4 — mixed (diurnal, broadband)

**Purpose.** Realistic traffic mix with a rising/bursting/falling shape.
Exercises all subsystems simultaneously. Headline workload for L4 CPU
throttling.

**Composition.**

| User class | Weight | Share |
|---|---|---|
| BrowseUser (W1 tasks) | 3 | ~60% of traffic |
| UploadUser (W2 tasks) | 1 | ~20% |
| GradeUser (W3 tasks) | 1 | ~20% |

**Diurnal shape** (`DiurnalShape` in locustfile, only active when
`WORKLOAD=W4-mixed`):

| Phase | Duration | Users | Description |
|---|---|---|---|
| Ramp-up | 0–5 min | 0 → target | morning arrival |
| Plateau | 5–15 min | target | steady teaching session |
| Burst | 15–16 min | target × 2 | end-of-class rush |
| Settle | 16–20 min | target | wind-down |
| Ramp-down | 20–25 min | target → 0 | close of day |

Total run time: **~26 minutes** (shape ends at 25 min + 1 min buffer).
`run.py` uses `w4_run_seconds: 1560` from `matrix.yaml`.

**Intensity axis** (target = plateau user count):

| Level | Target | Burst ceiling |
|---|---|---|
| low | 10 | 20 |
| medium | 30 | 60 |
| high | 60 | 120 |

**Used in.** E3-cpu-cap.

---

## Scenarios

### S0 — baseline (control)

**Layer.** None (all knobs at known-good values).

**Configuration.**

```
JAVA_TOOL_OPTIONS = -Xmx2g -Xms512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200
MYSQL_BUFFER_POOL = 1G
MYSQL_MAX_CONNS   = 200
QUARKUS_DB_POOL_MAX = 20
BACK_CPUS = 2.0   BACK_MEM = 2g
MYSQL_CPUS = 2.0  MYSQL_MEM = 2g
```

**Purpose.** Every perturbed scenario is compared against a matched S0 run
at the same workload and intensity. Without S0 you have no baseline to
measure the "how much worse" axis that makes results publishable.

**What to expect.** Clean JVM metrics (heap ratio <0.5, GC pauses rare),
low latency (p99 < 500ms for W2 at medium), zero container restarts,
`agroal_awaiting_count = 0`.

---

### S1 — heap-256m

**Layer.** L1 (JVM image configuration)
**Knob.** `JAVA_TOOL_OPTIONS → -Xmx`
**Perturbation.** `256m` (down from `2g`)
**Paired workload.** W2-upload, medium intensity (10 users)
**GC logging.** `-Xlog:gc*:/tmp/gc.log` enabled — snapshot captures the full log.

**What breaks and why.** The JVM gets 256 MB max heap. A single 10 MB PDF
upload can require 30–50 MB of heap (decode buffers, multipart parsing,
HTTP request object). With 10 concurrent uploaders, the JVM hits ceiling
constantly. G1GC starts doing frequent Full GCs; pause time climbs; HTTP
request threads block waiting for GC to free memory; p99 latency spikes.
If heap never recovers, the JVM throws `OutOfMemoryError` and the backend
returns 5xx.

**Diagnostic signals to watch.**

| Metric | Healthy (S0) | Degraded (S1) |
|---|---|---|
| `jvm_memory_used_bytes{area='heap'} / jvm_memory_max_bytes{area='heap'}` | < 0.5 | > 0.9 sustained |
| `rate(jvm_gc_pause_seconds_count[1m])` | < 0.1 /s | > 1 /s |
| `jvm_gc_pause_seconds_sum` rate | near 0 | rising sharply |
| HTTP p99 (Locust `locust_stats_history.csv`) | < 500ms | > 2500ms |
| Error rate | < 0.1% | > 1% |

**Ground-truth fix.** `JAVA_TOOL_OPTIONS=-Xmx2g` (restore baseline).

**How to use results.** Heap ratio × GC rate scatter (Fig 3 in
`analyze.ipynb`) shows the evidence link. A recommender seeing
`heap_ratio > 0.9 AND gc_rate > 1/s` should output:
`file: scenarios/S1-heap-256m.env, construct: JAVA_TOOL_OPTIONS,
proposed_value: -Xmx2g`.

---

### S4 — mysql-buffer-128m

**Layer.** L3 (MySQL server configuration)
**Knob.** `MYSQL_BUFFER_POOL → innodb_buffer_pool_size`
**Perturbation.** `128M` (down from `1G`)
**Paired workload.** W3-grade, all intensities

**What breaks and why.** The InnoDB buffer pool is MySQL's page cache.
At `1G` the entire working set (exam rows, student rows, question rows) fits
in memory — reads are served from RAM. At `128M` the pool is too small;
frequently accessed pages are evicted. Every grade-save read that misses
cache goes to disk. The grading hot path (`POST /api/student-responses`)
requires multiple lookups per request (exam sheet, question, existing
response). Under W3's 20–50 concurrent graders, each lookup is a disk read.
Query time rises 3–10× while CPU stays low and JVM heap is fine.

**Diagnostic signals to watch.**

| Metric | Healthy (S0) | Degraded (S4) |
|---|---|---|
| `rate(innodb_buffer_pool_reads[1m]) / rate(innodb_buffer_pool_read_requests[1m])` | < 0.01 (< 1%) | > 0.05 (> 5%) |
| Query p95 (Locust) | < 100ms | > 300ms |
| CPU (Micrometer `system.cpu.usage`) | moderate | unchanged or lower |
| JVM heap ratio | normal | normal |

**Ground-truth fix.** `MYSQL_BUFFER_POOL=1G`

**How to use results.** The cache-miss rate is the smoking gun. A
recommender seeing `miss_rate > 5%` at L3 with no JVM heap or CPU
signals should recommend increasing `innodb_buffer_pool_size`.

---

### S5 — mysql-conns-30

**Layer.** L3 (MySQL server configuration)
**Knob.** `MYSQL_MAX_CONNS → max_connections`
**Perturbation.** `30` (down from `200`; MySQL 8 default is 151)
**Paired workload.** W3-grade, high intensity (50 users)

**What breaks and why.** MySQL refuses new connections once
`threads_connected = max_connections`. With 50 concurrent graders each
holding a connection while executing a query, the ceiling of 30 is hit
immediately. Quarkus's Agroal pool (max 20) tries to acquire connections
from MySQL. Some succeed; the rest get "Too many connections" errors.
The app pool can't fill — `agroal_active_count` stays below 20, but new
requests queue (or time out). Error rate spikes.

**Diagnostic signals to watch.**

| Metric | Healthy (S0) | Degraded (S5) |
|---|---|---|
| `mysql_global_status_threads_connected` | < 40 | plateaus exactly at 30 |
| `mysql_global_status_aborted_connects` | 0 | rising |
| `agroal_active_count` | up to 20 | < 20, fluctuating |
| `agroal_awaiting_count` | 0 | > 0 (requests queuing) |
| Error rate (Locust) | < 0.1% | high (connection refused) |

**Ground-truth fix.** `MYSQL_MAX_CONNS=200`

**Key distinguisher vs S6.** `threads_connected` plateaus at a hard
integer equal to `max_connections`. The bottleneck is at the database
layer, not the app pool. MySQL `threads_running` can be near `threads_connected`.

---

### S6 — quarkus-pool-5

**Layer.** L3 (application configuration — Quarkus datasource)
**Knob.** `QUARKUS_DB_POOL_MAX → QUARKUS_DATASOURCE_JDBC_MAX_SIZE`
**Perturbation.** `5` (down from `20`)
**Paired workload.** W3-grade, medium intensity (20 users)

**What breaks and why.** Quarkus's Agroal connection pool caps at 5
active JDBC connections to MySQL. With 20 concurrent graders, 15 of them
are always waiting to acquire a connection from the pool. MySQL is not
under any load (< 5 threads running) — it has capacity to spare. The
bottleneck is entirely inside the JVM: requests queue in Agroal's
waiting list, p95 latency rises, throughput plateaus.

**Diagnostic signals to watch.**

| Metric | Healthy (S0) | Degraded (S6) |
|---|---|---|
| `agroal_active_count` | up to 20, dynamic | plateaus at exactly 5 |
| `agroal_awaiting_count` | 0 | > 0 sustained |
| `agroal_max_used_count` | up to 20 | = 5 (never exceeded) |
| `mysql_global_status_threads_running` | proportional to load | low (DB is idle) |
| `mysql_global_status_threads_connected` | proportional to load | ≤ 5 |

**Ground-truth fix.** `QUARKUS_DB_POOL_MAX=20`

---

### S5 vs S6 — The cross-layer trap

> This pair is the paper's central empirical contribution (P2 as well as P1).

Both produce the same user-facing symptom: slow grading, rising p95,
poor throughput under W3. A naive recommender — or a human without
diagnostic signals — cannot tell them apart from Locust stats alone.

| Signal | S5 (DB bottleneck) | S6 (App pool bottleneck) |
|---|---|---|
| `threads_connected` plateau | **at `max_connections` value** | at ≤ pool size (5) |
| `aborted_connects` rising | **yes** | no |
| `agroal_awaiting_count > 0` | transient | **sustained** |
| `threads_running` (MySQL) | near `threads_connected` | **low despite load** |
| Fix location | MySQL `.env`: `MYSQL_MAX_CONNS` | App `.env`: `QUARKUS_DB_POOL_MAX` |

A recommender that checks both signals can disambiguate with high
confidence. One that only checks user-facing p95 will guess randomly.

---

### S7 — cpu-cap-half

**Layer.** L4 (degenerate — Docker Compose resource limits)
**Knob.** `BACK_CPUS → deploy.resources.limits.cpus`
**Perturbation.** `0.5` CPU (down from `2.0`)
**Paired workload.** W4-mixed, all intensities

**What breaks and why.** Docker maps `BACK_CPUS=0.5` to a cgroup CPU
quota of 50ms per 100ms period (50% of one core). The Quarkus JVM is
multi-threaded; at medium intensity (30 users, burst to 60) it wants
~1.5 cores. The kernel's CFS scheduler enforces the quota and throttles
the container: threads are runnable but cannot execute. The JVM's HTTP
thread pool fills up; requests queue; p99 rises 3–5×. The host CPU is
not saturated — the machine has headroom, but the container is not
allowed to use it.

**Diagnostic signals to watch.**

| Metric | Healthy (S0) | Degraded (S7) |
|---|---|---|
| `rate(container_cpu_cfs_throttled_seconds_total{name='correctexam-back'}[1m])` | ~0 | **> 0.5 /s sustained** |
| `container_cpu_cfs_throttled_seconds_total / container_cpu_cfs_periods_total` (throttle ratio) | < 0.05 | > 0.3 |
| HTTP p99 (Locust) | < 500ms | 1500–3000ms |
| Throughput (req/s) | scales with users | plateaus early |
| Host CPU (unreliable on macOS) | headroom | headroom |

**Ground-truth fix.** `BACK_CPUS=2.0`

**Why this is the headline L4 scenario.** It is the closest analogue to
the classical cloud right-sizing problem (wrong instance type) and is
directly comparable to CherryPick-style Bayesian baselines. The
throttling rate metric is a unique fingerprint: no other failure mode
produces a rising `cfs_throttled_seconds` rate without host saturation.

---

### S8 — mem-cap-512m

**Layer.** L4 (degenerate — Docker Compose resource limits)
**Knob.** `BACK_MEM → deploy.resources.limits.memory`
**Perturbation.** `512m` (down from `2g`; below the JVM's own `Xmx=2g`)
**Paired workload.** W2-upload, medium intensity

**What breaks and why.** The JVM's `-Xmx2g` target exceeds the
container's 512 MB cgroup memory limit. When the JVM tries to commit
heap pages beyond 512 MB, the Linux OOM killer (enforcing the cgroup
limit) kills the container process. Uploads in flight are truncated.
Docker restarts the container. The JVM restarts cold with no warm caches.

**Key distinction from S1.** In S1, the JVM's own heap fills up
(`heap_ratio → 1.0`) before OOM. In S8, the heap has room (`heap_ratio`
stays healthy — the JVM thinks it has 2 GB) but the container is killed
by the kernel. The JVM never sees the problem; only the cgroup signals
show it.

**Diagnostic signals to watch.**

| Metric | S1 (JVM heap OOM) | S8 (cgroup OOM) |
|---|---|---|
| `jvm_memory_used / jvm_memory_max` (heap ratio) | **→ 1.0** | stays low (< 0.5) |
| `rate(jvm_gc_pause_seconds_count[1m])` | **high** | normal |
| `container_memory_failcnt{name='correctexam-back'}` | 0 | **> 0** |
| Container restart count | 0 (JVM catches it) | **rising** |

**Ground-truth fix.** `BACK_MEM=2g`

---

## Experiment matrix

| Experiment | Scenarios | Workload | Intensities | Key figure |
|---|---|---|---|---|
| E1-jvm-heap | S0, S1 | W2-upload | low, medium, high | heap ratio × GC rate scatter |
| E2-mysql-buffer | S0, S4 | W3-grade | low, medium, high | buffer pool miss rate time series |
| E2b-cross-layer-trap | S0, S5, S6 | W3-grade | medium, high | 3×2 disambiguation panel |
| E3-cpu-cap | S0, S7 | W4-mixed | low, medium, high | throttle rate × diurnal shape overlay |
| E4-mem-cap | S0, S8 | W2-upload | medium | failcnt vs heap ratio comparison |

---

## How to use the results

### Snapshot bundle (one per cell)

```
results/<run_id>/
  metadata.yaml             experiment, scenario, workload, intensity, iac_sha
  locust_stats.csv          per-endpoint totals (req count, failures, p50/p95/p99)
  locust_stats_history.csv  30s-interval time series of the above
  locust_failures.csv       individual failure details
  prom_snapshot.json        PromQL query_range for all matrix metrics
  gc.log                    JVM GC log from container (S1 only, -Xlog:gc*)
  mysql_status.txt          SHOW ENGINE INNODB STATUS snapshot
  docker_stats.txt          one-shot container resource reading
```

### Analysis notebooks

Each experiment has `experiments/E<n>-<name>/analyze.ipynb`. Open in
Jupyter, run all cells. Notebooks auto-discover runs via `metadata.yaml`
so they pick up any new cells automatically.

Figures saved to `experiments/E<n>-<name>/figures/` as PDF for paper inclusion.

### Recommender pipeline

The snapshot bundle is the **sole input** to the recommender — no live
cluster access. The pipeline is:

```
prom_snapshot.json + locust_stats.csv
        │
        ▼
recommender/diagnose.py   → identify which diagnostic signal fires
        │
        ▼
recommender/patch.py      → emit recommendation YAML (layer + file + construct + rationale + evidence)
        │
        ▼
apply patch manually      → edit .env, redeploy with run_scenario.sh
        │
        ▼
recommender/verify.py     → compare new snapshot against S0 baseline
                          → pass if: signal normalized + p99 within 1.2× S0
```

### Verification pass criteria

A patch passes iff all three hold:
1. The firing diagnostic signal returns to within tolerance of S0 baseline.
2. p99 latency and error rate return to within 1.2× of the S0 baseline at
   the same workload and intensity.
3. No new diagnostic signal starts firing (the patch did not break another
   layer).

Condition 3 is the one most often missed and is the sharpest empirical
test of the recommender's correctness.

### Recommendation output schema

```yaml
recommendation:
  layer: L1 | L3 | L4
  file: iac/stage1-compose/scenarios/S1-heap-256m.env
  construct: JAVA_TOOL_OPTIONS
  current_value: "-Xmx256m -Xms256m -XX:+UseG1GC -Xlog:gc*:/tmp/gc.log"
  proposed_value: "-Xmx2g -Xms512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
  rationale: |
    GC pause rate sustained at 2.3/s over the 5-minute measurement window
    and heap utilisation peaked at 0.92, both exceeding their diagnostic
    thresholds (> 1/s and > 0.90 respectively). Increasing -Xmx to 2g
    matches the observed working set and restores the headroom needed for
    concurrent PDF upload buffering.
  evidence:
    - metric: rate(jvm_gc_pause_seconds_count[1m])
      value_observed: 2.3 /s (mean over run window)
      threshold_violated: "> 1 /s"
    - metric: jvm_memory_used_bytes{area='heap'} / jvm_memory_max_bytes{area='heap'}
      value_observed: 0.92
      threshold_violated: "> 0.90"
  confidence: 0.94
```

The `evidence` field is what distinguishes this work from CherryPick
(no traceable evidence) and Lightspeed-style LLM completion (no runtime
grounding). It is the paper's central artifact.
