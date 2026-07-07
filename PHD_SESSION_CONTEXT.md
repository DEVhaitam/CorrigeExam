# PhD Session Context — CorrectExam IaC Experiments
> **For Claude desktop.** Paste this file at the start of a conversation to restore full context.
> Last updated: 2026-07-06. Node: parasilo-9.rennes.grid5000.fr.

---

## 1. Thesis in one paragraph

**Problem.** IaC (Infrastructure-as-Code) encodes static resource decisions whose runtime
behaviour is dynamic. There is no systematic way to translate observed runtime evidence
(system metrics, application metrics, load-test results) back into the specific IaC
construct that should change.

**Approach.** A typed mapping model `runtime observation × IaC context → patch`, instantiated
as a recommender that emits source-code patches with rationales, evaluated on a benchmark
of misconfiguration scenarios across multiple apps.

**Differentiation.** Existing work optimises config *values* (CherryPick, OtterTune) or
generates IaC code without runtime grounding (Lightspeed). This work operates on versioned
source artifacts with traceable evidence-to-patch links, across four IaC layers.

**Year.** End of Y2. Y3 starts September 2026. Two papers targeted in Y3.

---

## 2. IaC layer model

| Layer | Tools | Knobs | Monitoring scope |
|---|---|---|---|
| L1 image | Dockerfile, JVM opts | heap, GC mode, base image | app / process / container |
| L2 orchestration | K8s, Helm | requests, limits, replicas, affinity | pod / container / node |
| L3 config mgmt | Ansible, env files | DB pool, worker procs, cache, timeouts | service / cluster |
| L4 provisioning | Terraform, EnOSlib | instance type, count, network, region | node / VM / host |

**Bidirectional reading.** Top-down: a layer determines its monitoring scope. Bottom-up:
a monitoring scope reveals which layer might need to change. The thesis argues that
single-layer recommenders mis-localize when symptoms manifest above the actual layer
(e.g., a pod metric can point to either L1 or L4 depending on context).

---

## 3. Paper plan

### P1 — Evidence-grounded provisioning patches (active)
- **Target:** ICSE 2027 NIER (~Sept 2026 deadline) as stake-in-ground; full version FSE 2027 / ICSE 2028
- **One-line pitch:** Given runtime telemetry + IaC repo → minimal, rationale-annotated source-code patch at L4
- **Contributions:** mapping model (C1), recommender technique (C2), misconfiguration benchmark ~20 scenarios (C3), evaluation accuracy + SE-quality + dev study (C4)
- **Required experiments:** E1 JVM heap × W2, E2 MySQL buffer × W3, **E3 cgroup CPU × W4** (headline), E4 generalization (TeaStore, Sock Shop), E5 developer study
- **The Stage 1 experiments described in this document are the empirical foundation for E3**

### P2 — Cross-layer IaC reasoning (planned, Y3 second half)
- Multi-layer IaC project as typed graph; localize which layer to change given a symptom
- Depends on P1's mapping model and benchmark

---

## 4. Application under test: CorrectExam

A real-world 3-tier exam-grading platform (university use, ISTIC).

| Component | Stack | Port |
|---|---|---|
| Backend | Quarkus 3.x (JAX-RS, Hibernate ORM, Liquibase) | 8082 |
| Frontend | Angular 20 / nginx | 8080 |
| Database | MySQL 8.0 (InnoDB) | 3306 |
| File store | MinIO (S3-compatible) | 9000 |
| Metrics | Quarkus Micrometer → Prometheus | 9091 |

Key application characteristic: **the authentication endpoint is a synchronous JDBC
bottleneck** — every Locust user authenticates at scenario entry via `POST /api/authenticate`,
which triggers a blocking DB lookup + JWT signing. This endpoint dominates p95 latency at
high load regardless of provisioning. This is an important finding for the thesis: **no
IaC change can fix an application-level bottleneck** — it requires an L1 code change.

---

## 5. Experimental harness (Stage 1 — complete)

### Infrastructure

```
Laptop
  ├── grid5000/reserve.py         EnOSlib → books a Grid5000 bare-metal node
  └── ansible/                    Automates everything below

Grid5000 bare-metal node (parasilo-9.rennes.grid5000.fr)
  ├── Prometheus + Grafana         Monitoring, port 9092 / 3000
  ├── Local image registry         192.168.122.1:5000 (amd64 images, no DockerHub dependency)
  ├── Terraform (libvirt provider) Creates/destroys KVM VMs sequentially
  └── Locust                       Runs load scenarios against the active VM

Experiment VMs (one at a time, different RAM/CPU)
  └── Docker Compose
        ├── correctexam-back    :8082  (Quarkus)
        ├── correctexam-front   :8080  (Angular)
        ├── mysql:8.0           :3306
        ├── minio               :9000
        ├── node-exporter       :9100  (VM system metrics)
        ├── cadvisor            :8081  (container metrics — NOT working, see §7)
        └── mysqld-exporter     :9104  (MySQL metrics)
```

### Orchestration script: `grid5000/scripts/run_experiment.py`

Full cycle per config: Terraform apply → Ansible deploy_app + seed_db → Prometheus target update → Locust load ramp → Terraform destroy → save results.

Results layout: `lab/results/<config>/<run_id>/u<N>_stats.csv`, `u<N>_system.csv`, `summary.json`

Key fields in `summary.json`:
- `steps[].status`: `"ok"` / `"saturated"` / `"locust_error"` (hard infra failure)
- `breaking_point_users`: load where thresholds were crossed (p95 > 30 s or failure_rate > 5%)
- `infra_failure_users`: load where Locust exited non-zero (hard infra ceiling)

### Locust scenarios

| File | Type | What it stresses |
|---|---|---|
| s01_browse.py | Read-only baseline | Buffer pool, HTTP serialisation |
| s02_grade.py | Write-heavy | InnoDB WAL flush, Hikari pool |
| **s03_mixed.py** | **Grid5000 reference** | Everything — 60% read / 40% write |
| s04_import.py | Course import burst | Bulk INSERTs, JVM heap, long transactions |

**S03 is the main experiment scenario.** Steps: 10 → 25 → 50 → 100 → 200 → 500 → 1000 → 2000 → 4000 users × 10 min each. Stop if failure_rate > 5% or p95 > 30 s.

### Analysis script: `lab/analysis/analyse.py`

Generates 16 PNG figures in `lab/analysis/figures/`. Run with:
```bash
python3 lab/analysis/analyse.py
```

---

## 6. Stage 1 experiment results (2026-07-05 / 2026-07-06, parasilo-9)

### Experiment matrix

| Config | Phase | vCPU | RAM | Status | Canonical run |
|---|---|---|---|---|---|
| 2cpu-512mb | 1 | 2 | 512 MB | **OOM at startup** — MySQL never initialised | — |
| 2cpu-1gb   | 1 | 2 | 1 GB  | **OOM at startup** — MySQL never initialised | — |
| 2cpu-2gb   | 1 | 2 | 2 GB  | **Partial** — valid through u500 (no summary.json due to old bug) | 20260705_235436 |
| 2cpu-4gb   | 1 | 2 | 4 GB  | **Complete** — ran through u2000 | 20260706_011803 |
| 2cpu-8gb   | 1 | 2 | 8 GB  | **Partial** — transient locust exit at u500 | 20260706_024427 |
| 1cpu-4gb   | 2 | 1 | 4 GB  | **Partial** — real CPU saturation at u500 | 20260706_035028 |
| 4cpu-4gb   | 2 | 4 | 4 GB  | **Complete** — ran full sweep to u4000 | 20260706_045638 |
| 8cpu-4gb   | 2 | 8 | 4 GB  | **Complete** — ran full sweep to u4000 | 20260706_063236 |

### Stable capacity per config (p95 < 1 s, failure rate < 0.1%)

| Config | Stable users | Notes |
|---|---|---|
| 1cpu-4gb | 200 | CPU 84% at u200; hits 99% at u500 |
| **2cpu-2gb** | **500** | CPU 89%, RAM 69% at u500 — tight but within SLO |
| **2cpu-4gb** | **500** | CPU 89% at u500; p95 = 570 ms |
| 2cpu-8gb | 500 ▶ | Partial — locust transient error at u500 (1 failure / 126K reqs) |
| 4cpu-4gb | 500 | CPU 48% — not CPU-limited; MySQL QPS at 48% ceiling |
| 8cpu-4gb | 1000 | CPU 40% at u1000; MySQL QPS 90% ceiling |

### Peak throughput observed

| Config | Peak RPS | At users |
|---|---|---|
| 1cpu-4gb | 144 | u500 (infra fail) |
| 2cpu-2gb | 207 | u500 |
| 2cpu-4gb | 215 | u1000 |
| 2cpu-8gb | 211 | u500 (infra fail) |
| 4cpu-4gb | 384 | u1000 |
| 8cpu-4gb | 463 | u4000 |

---

## 7. Key findings from Stage 1

### Finding 1 — RAM is NOT the bottleneck above 2 GB
All 2-vCPU configs (2 GB / 4 GB / 8 GB) deliver identical throughput curves and p95
curves. Prometheus shows they all consume **~1.4–2.0 GB RAM absolute** regardless of
allocation. 2 GB stays at 68–74% RAM utilisation — sufficient but tight with no headroom
for JVM GC spikes. **4 GB is the sweet spot: same performance, twice the headroom.**

> IaC recommendation implication: do not recommend >4 GB RAM for this workload at this
> scale — it provides no performance benefit.

### Finding 2 — CPU is the bottleneck for ≤2 vCPU configs
1 vCPU hits 99% CPU at u500; 2 vCPU hits 99% at u1000. The CPU saturation curve
(Fig 13) shows all three 2-vCPU configs converging on the same curve — confirming RAM
plays no role. 1 vCPU shows measurable latency pressure even at u50 (p95 = 130 ms vs
60 ms for 2 vCPU).

### Finding 3 — MySQL connection pool caps throughput above 4 vCPUs
`mysql_threads_connected` is constant at **9 connections** across all configs and all
load levels. This is the Quarkus JDBC pool size (likely set in `application.properties`).

- 2→4 vCPU: +78% throughput gain (CPU was the bottleneck)
- 4→8 vCPU: only +21% gain (MySQL pool is now the bottleneck)
- At 4+ vCPUs, MySQL QPS plateaus at ~4 800–5 000 QPS while CPU stays at 45–70%

> IaC recommendation implication: adding more vCPUs beyond 4 is not the right patch.
> The right fix is at L3 (increase DB pool size in `application.properties`) or L1
> (switch to reactive / non-blocking DB access). This is a cross-layer case — the
> monitoring signal (MySQL thread saturation visible at the VM level) points to an L3
> fix, not an L4 fix.

### Finding 4 — Authentication is an application-level bottleneck
`POST /api/authenticate` consistently dominates p95 latency at all load levels. At u500
(2cpu-4gb): p95 = 21–34 s while all other endpoints stay below 500 ms. This is a
synchronous JDBC lookup + JWT signing on every login — no amount of infrastructure
scaling can fix it.

> For the IaC paper: this is a clean example of a misconfiguration that is *not* at L4.
> An L4 recommender would mis-localize this as "add more CPU" — which is wrong.

### Finding 5 — OOM floor: 2 GB is the minimum viable allocation
2cpu-512mb and 2cpu-1gb never started — the Linux OOM killer terminated MySQL before
InnoDB initialisation completed. 2 GB is the confirmed minimum floor.

---

## 8. What is known NOT to work (cadvisor)

The `cadvisor` container metrics exporter (`correctexam-mysql`, `correctexam-back`
container CPU/RAM via port 8081) returns **no data**. The `mysql_cpu_pct`, `back_cpu_pct`,
`mysql_mem_bytes`, `back_mem_bytes` columns in `_system.csv` files are all empty.
Available metrics: VM-level (node-exporter), JVM heap (Quarkus Micrometer), MySQL
(mysqld-exporter). cadvisor is deployed but the container name labels don't match.

---

## 9. What is next

### Experiments to complete
- **Re-run `2cpu-8gb`** — the u500 stop was a transient locust exit (1 failure / 126K requests, CPU only 87%). With the fix in place (locust exit 1 now records the step and continues), re-running should get to u1000+. This will confirm whether 2cpu-8gb truly matches 2cpu-4gb or has higher capacity.
- **Re-run `2cpu-2gb`** — has data through u500 but no summary.json. Re-running gets u1000+ and a clean summary. This completes the RAM sweep.
- **1cpu-4gb does NOT need re-running** — CPU saturation is confirmed (99% at u500, p95 = 2800 ms). The stable capacity of 200 users is definitive.
- **S04 import scenario** — not yet run on Grid5000. Planned for a subset of configs (2cpu-2gb, 2cpu-4gb, 8cpu-4gb). Tests ingestion-phase behavior (DB grows during run).

### Analysis to extend
- **Endpoint-level analysis with new data** — update Fig 7 for the completed configs
- **JVM heap time-series** — use `_system.csv` jvm_heap_bytes to show GC pressure curves
- **Cross-scenario comparison** — when S04 runs are available, compare S03 vs S04 bottleneck profiles (S03 = steady-state, S04 = write-burst)

### Paper work
- Draft E3 experiment section (methodology + results from Stage 1)
- Formulate the L4 patch recommendations grounded in the data (the mapping model instantiation)
- Identify 2–3 misconfiguration scenarios for the benchmark (e.g., `2cpu-2gb` → recommend `2cpu-4gb`; `8cpu-4gb` → flag MySQL pool as real bottleneck, recommend L3 fix)
- Write the cross-layer trap argument using Finding 3 (MySQL pool) and Finding 4 (auth bottleneck)

---

## 10. Infrastructure cheat sheet

```bash
# Reserve a node (laptop)
python3 grid5000/reserve.py --site rennes --walltime 06:00:00
# Reuse same node
python3 grid5000/reserve.py --site rennes \
  --node $(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")

# Bootstrap node — first time (~45 min)
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/setup_node.yml

# SSH tunnel for monitoring (laptop)
NODE=$(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")
ssh -N -L 3000:localhost:3000 -L 9092:localhost:9092 root@$NODE &

# Run experiments (on the node)
# All remaining (skip completed ones):
python3 grid5000/scripts/run_experiment.py --all --skip 2cpu-4gb --skip 4cpu-4gb --skip 8cpu-4gb
# Single config:
python3 grid5000/scripts/run_experiment.py --config 2cpu-8gb
# Phase only:
python3 grid5000/scripts/run_experiment.py --phase 1

# Collect results (laptop)
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/collect_results.yml

# Re-run analysis (laptop)
python3 lab/analysis/analyse.py
```

---

## 11. File map (most relevant files)

| File | Purpose |
|---|---|
| `PHD_CONTEXT.md` | Stable thesis framing (update only on major pivots) |
| `PAPERS.md` | Paper structures and open issues |
| `PROGRESS.md` | Living task log (NOTE: outdated — superseded by this file for experiments) |
| `grid5000/experiments.yml` | Experiment matrix (VM configs, load steps, saturation thresholds) |
| `grid5000/scripts/run_experiment.py` | Full experiment orchestrator |
| `lab/scenarios/s03_mixed.py` | Main Locust scenario |
| `lab/results/<config>/<run_id>/` | Raw experiment output (CSVs + summary.json) |
| `lab/analysis/analyse.py` | Generates all 16 analysis figures |
| `lab/analysis/figures/` | 16 PNG figures (Figs 1–16) |
| `grid5000/terraform/` | Terraform libvirt provider config |
| `ansible/playbooks/` | deploy_app, seed_db, setup_node, collect_results |

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Cell** | One (scenario, workload, deployment) tuple in the experiment matrix |
| **iac_sha** | Git SHA of the IaC artifacts used for a run — ties observations to the exact config |
| **Mapping model** | `(signal, value, window, target) × (layer, file, construct, value, location) → {patch, rationale, confidence}` |
| **Stable capacity** | Max users where p95 < 1 s and failure rate < 0.1% |
| **Infra failure** | Locust exits non-zero but produced a CSV — hard ceiling, not threshold crossing |
| **Partial run** | Config stopped before completing the full user sweep — result is a lower bound |
| **s03_mixed** | 60% read (browse courses/exams/sheets) / 40% write (grade, submit) — the reference workload |
| **L4 → L3 cross-layer trap** | MySQL pool saturates at 9 connections; naive diagnosis says "add CPU" (L4) but real fix is pool size (L3) |