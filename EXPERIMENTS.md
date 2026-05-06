# EXPERIMENTS

Operational spec for Claude Code. Each section is self-sufficient — Claude
Code can scaffold the corresponding files from any single block without
loading the others.

Read order for Claude Code: §1 Conventions → §2 Workloads → §3 Scenarios
→ §4 Recommendation loop. Generate files only as requested per session;
do not generate everything in one pass.

---

## §1 Conventions

### Stages

- **S0** — local docker-compose on developer laptop (macOS Docker Desktop).
  Iterate here.
- **S1** — single Grid'5000 node, docker-compose under EnOSlib.
- **S2** — three Grid'5000 bare nodes, EnOSlib + Ansible (planned).
- **S3** — k3s cluster on Grid'5000 (planned).

S0 is the only stage that exists today for workload + scenario iteration.
All §3 scenarios target S0 first; promotion to S1+ is mechanical (same
parameters, different runner).

### IaC layers covered in S0

S0 lacks orchestration (no K8s), so L2 is deferred to S3. S0 covers:
- **L1 image** — JVM flags via `JAVA_TOOL_OPTIONS`, base image
- **L3 config** — service config knobs (Quarkus env vars, MySQL command flags)
- **L4 (degenerate)** — docker-compose `deploy.resources.limits.cpus`,
  `deploy.resources.limits.memory`

### Compose layering (the IaC artifact under study)

S0 deployments use three files together:

1. **`docker-compose.yml`** — upstream CorrectExam stack, unmodified.
2. **`iac/stage1-compose/research-overrides.yml`** — adds: a
   `JAVA_TOOL_OPTIONS` env on `back`, parameterized resource limits on
   `back` and `mysql`, parameterized MySQL command flags
   (`innodb_buffer_pool_size`, `max_connections`), `QUARKUS_DATASOURCE_JDBC_MAX_SIZE`
   env on `back`, the `mysqld_exporter` service, and `MINIO_PROMETHEUS_AUTH_TYPE=public`
   on `minio`.
3. **`iac/stage1-compose/scenarios/<S>.env`** — per-scenario values.

The recommender's patches target either the override file (for adding new
knobs) or the .env file (for changing existing knob values). Per-scenario
diff is exactly the variables that changed — typically one line.

### macOS validity caveat

You're on Docker Desktop, which runs Linux in a VM. cAdvisor sees
container cgroups inside the VM (accurate) but `node-exporter` sees the
VM not the host (inaccurate). This means:

- **Reliable on macOS:** Quarkus Micrometer, mysqld_exporter, MinIO
  metrics, cAdvisor (container CPU, memory, throttling, OOM events),
  Locust workload-side measurements.
- **Unreliable on macOS:** `node_cpu_seconds_total`, `node_memory_*`,
  per-CPU saturation. Defer scenarios that depend on host metrics
  (e.g., cpuset pinning conflicts) to Grid'5000.

All §3 scenarios were chosen to be diagnosable from reliable metrics on
macOS S0.

### Mandatory labels

Every Prometheus series, every Locust CSV MUST carry:

- `experiment_id` (UUID per run, set in the runner)
- `scenario` (matches the .env filename stem, e.g. `S1-heap-256m`)
- `iac_sha` (`git rev-parse --short HEAD` at deploy time)
- `workload` (`W1-browse` / `W2-upload` / `W3-grade` / `W4-mixed`)
- `intensity` (`low` / `medium` / `high`)

Prometheus carries these via `external_labels` in `prometheus.yml`
(`PROM_EXTERNAL_*` vars in scenario .env files). Locust carries them
via env vars read by the locustfile.

Code paths that drop these labels are bugs — they break the join between
runtime observations and IaC code.

### Run lifecycle

```
warmup (30s) → steady-state measurement (5m) → cooldown (15s) → snapshot
```

Snapshots include: Locust CSVs (stats / history / failures), PromQL
query_range JSON over the run window, JVM GC log fetched from container,
container `docker stats` final reading, MySQL `SHOW ENGINE INNODB STATUS`
snapshot. Output goes to `results/<experiment>/<run_id>/`.

### Endpoints (per actual compose port mapping)

| Service | URL | Purpose |
|---|---|---|
| Front (UI) | http://localhost:8080 | Angular UI |
| Back (API) | http://localhost:8082 | Quarkus REST |
| Quarkus metrics | http://localhost:8082/q/metrics | confirm with curl on first run |
| Prometheus | http://localhost:9092 | targets at /targets |
| Grafana | http://localhost:3000 | admin / admin |
| mysqld_exporter | http://localhost:9104/metrics | added by research overrides |
| MinIO console | http://localhost:9090 | admin / minioadmin |

The Locust runner targets `http://localhost:8082` for the back end.

---

## §2 Workloads

Four workload profiles, implemented as separate Locust `User` classes in
`workload/locust/locustfile.py`. The `--tags` CLI flag selects which
profile(s) to activate per run.

### W1 — browse (control, light)

**Tag:** `browse`
**Stresses dominantly:** JVM CPU, MySQL read path, query cache.
**Use as control for:** L1 heap, L4 CPU. Should show minimal change under
those perturbations because the working set is small and reads cache well.

**Endpoints (verify against JHipster controllers):**
| Method | Path | Weight | Notes |
|---|---|---|---|
| POST | `/api/authenticate` | once on `on_start` | get JWT, attach as Bearer |
| GET | `/api/courses?page=0&size=20` | 5 | list view |
| GET | `/api/exams?page=0&size=20` | 4 | list view |
| GET | `/api/students?page=0&size=50` | 3 | list view |
| GET | `/api/exams/{id}` | 2 | detail view, id from prior list |
| GET | `/q/health` | 1 | cheap baseline ping |

**Wait pattern:** `between(1, 3)` seconds.
**Intensity axis:** `users ∈ {5, 20, 50}`, ramp `r=2` per second.

### W2 — upload (heap + I/O dominant)

**Tag:** `upload`
**Stresses dominantly:** JVM heap (PDFs buffered before stream), MinIO
write bandwidth, network throughput.
**Headline workload for:** L1 `-Xmx` perturbations.

**Endpoints (verify exact paths against backend):**
| Method | Path | Weight | Notes |
|---|---|---|---|
| POST | `/api/authenticate` | once | get JWT |
| POST | `/api/exams` | 1 | create new exam (cheap) |
| POST | `/api/exam-sheets` (multipart) | 6 | THE hot path — uploads PDF |
| GET | `/api/exam-sheets/{id}` | 1 | verify upload |

If the upload endpoint differs in the actual JHipster controllers, fix
the path and the multipart field name in the locust task — keep the weights.

**Payload size distribution.** Generate synthetic PDFs once into
`workload/data/synthetic-exams/` at startup:
- 70% of uploads use a 200KB PDF
- 25% use a 2MB PDF
- 5% use a 10MB PDF

Skewed distribution matches teacher behavior more than uniform.

**Wait pattern:** `between(2, 5)` seconds (uploads aren't bursty).
**Intensity axis:** `users ∈ {3, 10, 25}`, ramp `r=1`. Lower than W1
because each request is heavier.

### W3 — grade (DB write-heavy)

**Tag:** `grade`
**Stresses dominantly:** MySQL write path, lock contention on shared exam
rows, result-aggregation read queries.
**Headline workload for:** L3 `innodb_buffer_pool_size`, MySQL `max_connections`,
Quarkus DB pool.

**Endpoints (verify against backend):**
| Method | Path | Weight | Notes |
|---|---|---|---|
| POST | `/api/authenticate` | once | get JWT |
| GET | `/api/exam-sheets?examId={id}` | 2 | fetch sheets to grade |
| GET | `/api/questions?examId={id}` | 1 | fetch question structure |
| PATCH or POST | `/api/answer-{type}/{id}` | 8 | THE hot path — save grade |
| GET | `/api/exams/{id}/results` | 1 | aggregation query (heaviest) |

The exact entity name for "answer" depends on CorrectExam's schema —
likely `text-comments`, `gradedComments`, or similar. The locust file
should expose this as a constant for fast adjustment.

**Wait pattern:** `between(0.5, 2)` seconds (graders click fast).
**Intensity axis:** `users ∈ {5, 20, 50}` ramp `r=2`. Concurrency
matters here — lock contention only manifests at scale.

### W4 — mixed (realistic, broadband)

**Tag:** `mixed`
**Stresses:** everything proportionally with diurnal pattern.
**Headline workload for:** L4 `cpus:` / `mem:` perturbations.

**Composition (per simulated user, weighted):**
- 60% browse tasks (subset of W1)
- 20% upload tasks (subset of W2)
- 20% grade tasks (subset of W3)

**Diurnal pattern (use Locust's `LoadTestShape`):**

| Phase | Duration | Users |
|---|---|---|
| ramp-up | 5 min | 0 → target |
| plateau | 10 min | target |
| burst | 1 min | target × 2 |
| settle | 4 min | target |
| ramp-down | 5 min | target → 0 |

Target intensity axis: `target ∈ {10, 30, 60}`.

The burst phase exposes scenarios that pass at steady state but fail
under spikes.

### Implementation notes for locustfile.py

- Use `@tag()` decorators so users invoke profiles via `--tags browse` /
  `--tags upload` / `--tags grade` / `--tags mixed`.
- Read `EXPERIMENT_ID`, `SCENARIO`, `WORKLOAD`, `INTENSITY` from env;
  expose them as labels in `request_meta` listener so Locust CSVs carry them.
- Auth token is per-user, fetched once in `on_start`. Re-auth on 401.
- Synthetic PDF generation: a one-shot helper in
  `workload/data/generate_pdfs.py` that produces three sizes once.
  Don't regenerate per run.

---

## §3 Scenarios

Format per scenario block:

```
### S<id> — <name>
Layer:    L<n>
Knob:     <field in .env>
Bad:      <wrong value>
Good:     <known correct value>
Workload: W<n> at intensity <low|med|high>
Symptom:  <which metric(s) deviate, expected direction>
Diagnostic signal: <metric pattern that uniquely identifies this layer>
Ground-truth fix: <exact patch — one line>
Verify:   <metric must return to baseline ± tolerance>
```

The "Diagnostic signal" field is critical — it's what your recommender
must learn to identify. Two scenarios with the same symptom but different
diagnostic signals are exactly the cross-layer trap.

---

### S0 — baseline (control)

**Layer:** none — this is the control.
**All knobs:** at known-good values; see `scenarios/S0-baseline.env`.
**Workload:** all four, all intensities.
**Symptom:** none — defines what "right" looks like.
**Use:** every other scenario's "Verify" step compares against this.

### S1 — heap-256m

**Layer:** L1
**Knob:** `JAVA_TOOL_OPTIONS` env var on `back` service
(honored by JVM regardless of entrypoint, unlike `JAVA_OPTS_APPEND`)
**Bad:** `-Xmx256m -Xms256m -XX:+UseG1GC -Xlog:gc*:/tmp/gc.log`
**Good:** `-Xmx2g -Xms512m -XX:+UseG1GC -XX:MaxGCPauseMillis=200`
**Workload:** W2 upload at medium intensity
**Symptom:** p99 latency >5× baseline, request error rate >1%
**Diagnostic signal:** `rate(jvm_gc_pause_seconds_count[1m]) > 1/s` AND
`jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 0.9`
sustained over the run window.
**Ground-truth fix:** edit `.env`: `JAVA_TOOL_OPTIONS=-Xmx2g -Xms512m -XX:+UseG1GC`
**Verify:** GC pause rate returns below 0.2/s, p99 within 1.2× baseline.

### S4 — mysql-buffer-128m

**Layer:** L3
**Knob:** `MYSQL_BUFFER_POOL` → `--innodb_buffer_pool_size`
**Bad:** `128M` (matches MySQL 8 default)
**Good:** `1G`
**Workload:** W3 grade at medium intensity
**Symptom:** query latency p95 >3× baseline, throughput drops despite
unsaturated CPU.
**Diagnostic signal:** `rate(mysql_global_status_innodb_buffer_pool_reads[1m])
/ rate(mysql_global_status_innodb_buffer_pool_read_requests[1m]) > 0.05`
(disk reads as fraction of all read attempts — i.e. cache miss rate >5%).
With `1G`, this stays near 0.
**Ground-truth fix:** edit `.env`: `MYSQL_BUFFER_POOL=1G`
**Verify:** cache miss rate <1%, query p95 within 1.2× baseline.

### S5 — mysql-conns-30

**Layer:** L3
**Knob:** `MYSQL_MAX_CONNS` → `--max_connections`
**Bad:** `30` (well below MySQL 8 default of 151)
**Good:** `200`
**Workload:** W3 grade at high intensity (must exceed connection ceiling)
**Symptom:** request error rate spikes, "Too many connections" errors in
back-end logs, sudden throughput cliff at the connection limit.
**Diagnostic signal:** `mysql_global_status_threads_connected` plateaus
exactly at `mysql_global_variables_max_connections` value, with rising
`mysql_global_status_aborted_connects`. (No other layer's failure mode
plateaus at a fixed integer like this.)
**Ground-truth fix:** edit `.env`: `MYSQL_MAX_CONNS=200`
**Verify:** `threads_connected < 80%` of max, no `aborted_connects`.

### S6 — quarkus-pool-5

**Layer:** L3
**Knob:** `QUARKUS_DATASOURCE_JDBC_MAX_SIZE` env var on `back` service
**Bad:** `5`
**Good:** `20` (Quarkus default)
**Workload:** W3 grade at medium intensity
**Symptom:** in-flight queries plateau at 5, request queue grows, p95
latency rises while DB itself is idle.
**Diagnostic signal:** `agroal_active_count` plateaus at 5,
`agroal_awaiting_count > 0` sustained, `agroal_max_used_count = 5`.
The DB-side `mysql_global_status_threads_running` is far below capacity.
This is the inverse pattern of S5 — bottleneck is in the *app* pool,
not the DB itself.
**Ground-truth fix:** edit `.env`: `QUARKUS_DB_POOL_MAX=20`
**Verify:** `agroal_awaiting_count` returns to 0, p95 normalizes.

> **S5/S6 is the cross-layer trap demo.** Same user-facing symptom
> (slow queries, high p95), opposite layer fixes. Without the diagnostic
> signal, a recommender can't tell them apart. **This pair alone may be
> the most important figure in paper 1.**

### S7 — cpu-cap-half

**Layer:** L4 (degenerate, via compose `deploy.resources.limits.cpus`)
**Knob:** `BACK_CPUS`
**Bad:** `0.5`
**Good:** `2.0`
**Workload:** W4 mixed at medium intensity
**Symptom:** p99 latency rises by 3–5×, throughput plateaus far below
baseline, host shows headroom.
**Diagnostic signal:**
`rate(container_cpu_cfs_throttled_seconds_total{name="correctexam-back"}[1m]) > 0.5`
sustained — unique fingerprint of cgroup CPU throttling. Host CPU is
*not* saturated, distinguishing this from "we need a bigger node."
cAdvisor reads cgroups inside the VM, so this signal is reliable on macOS.
**Ground-truth fix:** edit `.env`: `BACK_CPUS=2.0`
**Verify:** throttling metric returns to ~0, p99 within 1.2× baseline.

This is the headline L4 scenario: closest analogue to the AWS
instance-type problem and directly comparable to CherryPick-style
baselines.

### S8 — mem-cap-512m

**Layer:** L4
**Knob:** `BACK_MEM`
**Bad:** `512m` (smaller than JVM Xmx of 2g)
**Good:** `2g`
**Workload:** W2 upload at medium intensity
**Symptom:** OOM killer fires, container restarts, uploads truncated.
**Diagnostic signal:** `container_memory_failcnt > 0` OR container
restart count rises during the run. Distinguished from S1 by the fact
that JVM heap is healthy (room within Xmx) — the kill comes from the
*container* layer, not the JVM.
**Ground-truth fix:** edit `.env`: `BACK_MEM=2g`
**Verify:** no `failcnt`, container uptime > run duration.

---

## §4 Recommendation loop

The end-to-end pipeline that the recommender must implement and that the
evaluation harness must measure.

```
1. Inject       pick scenario S<n>; render .env from scenarios/<n>.env
        v
2. Deploy + run bring stack up; run paired W<n> at chosen intensity
        v
3. Snapshot     Locust CSVs + PromQL query_range + GC log + DB status
        v
4. Diagnose     evaluate diagnostic signals from §3; identify layer + knob
        v
5. Patch        generate IaC patch (file + line + new value + rationale)
        v
6. Apply + rerun apply patch; redeploy; rerun same workload + intensity
        v
7. Verify       diagnostic signal normalized? SLO compliance returned?
```

### Step 4 (Diagnose) — what the recommender consumes

A snapshot bundle: `(metadata.yaml, locust_*.csv, prom_snapshot.json,
gc.log, mysql_status.txt)`. The recommender must work from this bundle
alone — no live cluster access. This makes it reproducible and means
your benchmark IS a static dataset of bundles + ground-truth fixes.

### Step 5 (Patch) — output schema

```yaml
recommendation:
  layer: L1 | L3 | L4
  file: <relative path in repo, e.g. iac/stage1-compose/scenarios/S1-heap-256m.env>
  construct: <the env var or YAML key>
  current_value: <as observed in the file>
  proposed_value: <the patch>
  rationale: |
    Free-text explanation citing the diagnostic signal.
    E.g.: "GC pause rate sustained at 2.3/s over 5min and heap utilization
    >92% indicates JVM heap saturation under W2 upload load. Increasing
    -Xmx to 2g provides headroom matching the observed working set."
  evidence:
    - metric: jvm_gc_pause_seconds_count
      value_observed: 2.3 per second (mean over run)
      threshold_violated: > 1 per second
    - metric: jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"}
      value_observed: 0.92
      threshold_violated: > 0.90
  confidence: 0.0–1.0
```

This schema is paper 1's central artifact. The `evidence` field is what
distinguishes the work from CherryPick (no traceable evidence) and from
Lightspeed (no runtime grounding).

### Step 7 (Verify) — pass criteria

A patch passes verification iff:

1. The diagnostic signal from §3 returns to within tolerance of baseline.
2. SLO metric (workload-defined p99 + error rate) returns to within
   1.2× of S0 baseline at the same intensity.
3. No new diagnostic signal from another scenario starts firing
   (i.e. the patch didn't break a different layer).

Condition 3 is the one most often missed and is what makes the
verification a real test.

---

## §5 Files to scaffold (request individually)

Claude Code should generate these on request, one block at a time. Don't
generate everything in one session — pick the next thing in flight per
`STATUS.md`.

- `iac/stage1-compose/research-overrides.yml` — additive layer on the
  upstream `docker-compose.yml`. Adds JVM opts handle, resource limits,
  parameterized MySQL command, mysqld_exporter, MinIO scrape auth bypass.
- `iac/stage1-compose/prometheus.yml` — research-side scrape config.
- `iac/stage1-compose/scenarios/<S>-<name>.env` — one .env per scenario.
- `iac/stage1-compose/run_scenario.sh` — convenience wrapper for `up/down`.
- `workload/data/generate_pdfs.py` — synthetic PDF generator (3 sizes).
- `workload/locust/locustfile.py` — extend with W2 + W3 + W4 TaskSets +
  `LoadTestShape` for W4 diurnal.
- `workload/locust/auth.py` — JWT auth helper (factor out of locustfile).
- `experiments/E<n>-<layer>/run.py` — per-experiment runner that walks a
  matrix of (scenario × intensity).
- `experiments/E<n>-<layer>/analyze.ipynb` — notebook that ingests
  `results/` for that experiment and produces paper-figure plots.
- `recommender/diagnose.py` — Step 4: rule-based diagnostic signal eval.
- `recommender/patch.py` — Step 5: emit recommendation YAML.
- `recommender/verify.py` — Step 7: check post-patch behavior.

When generating a scenario .env, ALSO add an entry to
`experiments/matrix.yaml` so the runner picks it up automatically.

---

## §6 Validation before scaling

Before treating §3 as the official benchmark, manually validate three
things on a single laptop S0 deployment:

1. **Each "Bad" value actually breaks the SLO** at the chosen intensity.
   If S5 at high intensity doesn't actually saturate connections, the
   intensity is too low — bump it.
2. **Each "Good" value actually meets the SLO** under the same workload.
   If even the baseline fails, the SLO is too tight or the workload is
   too aggressive for the test hardware.
3. **The diagnostic signal is unique enough** to distinguish each scenario.
   Pairwise: take traces from S1 and S7 (both cause p99 spikes) — does
   the diagnostic signal correctly identify each? If not, the signal
   needs refinement before it goes in the recommender.

This is one or two days of work and saves weeks of bad data later.