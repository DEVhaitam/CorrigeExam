# Project Progress

> **Purpose** — Living document. Update this file whenever something is completed,
> started, or decided. Read it at the start of every session to restore context.
>
> **PhD context** — Evidence-grounded IaC patch generation for a Quarkus/MySQL
> exam-grading platform (CorrectExam / GradeScope ISTIC). The experiments measure
> how provisioning parameters (vCPU, RAM) affect performance under realistic
> workloads. Results will ground IaC recommendations in the thesis.

---

## Architecture snapshot

```
Laptop
  ├── reserve.py            EnOSlib → books a Grid5000 bare-metal node
  └── ansible/              Automates everything below

Grid5000 bare-metal node  (x86_64, KVM-capable)
  ├── Prometheus + Grafana  Docker containers, always running on the node
  ├── Local image registry  192.168.122.1:5000  (solves ARM cross-build issue)
  ├── Terraform (libvirt)   Creates/destroys experiment VMs sequentially
  └── Locust                Runs scenarios against the active VM

Experiment VMs  (one at a time, different RAM/CPU configs)
  └── Docker Compose
        ├── correctexam-back   Quarkus 3.x REST API  :8082
        ├── correctexam-front  Angular 20 / nginx     :8080
        ├── mysql:8.0          Database               :3306
        ├── minio              S3-compatible store    :9000
        ├── node-exporter      System metrics         :9100
        ├── cadvisor           Container metrics      :8081
        └── mysqld-exporter    MySQL metrics          :9104
```

---

## Experiment matrix (`grid5000/experiments.yml`)

| Label | Phase | vCPU | RAM | Goal |
|---|---|---|---|---|
| 2cpu-512mb | 1 | 2 | 512 MB | Below JVM min heap — expect fast failure |
| 2cpu-1gb   | 1 | 2 | 1 GB  | Tight heap — GC pressure |
| 2cpu-2gb   | 1 | 2 | 2 GB  | Near threshold |
| 2cpu-4gb   | 1 | 2 | 4 GB  | Comfortable — also Phase 2 control |
| 2cpu-8gb   | 1 | 2 | 8 GB  | Headroom — confirm plateau |
| 1cpu-4gb   | 2 | 1 | 4 GB  | Single-threaded bottleneck |
| 4cpu-4gb   | 2 | 4 | 4 GB  | Parallelism gain |
| 8cpu-4gb   | 2 | 8 | 4 GB  | Diminishing returns? |

Each config runs **s03_mixed** at load steps `[10, 25, 50, 100, 200, 500, 1000, 2000, 4000]` users × 5 min,
stopping at the first step where failure rate > 5 % or p95 > 30 s.

---

## Workload scenarios (`lab/scenarios/`)

| File | Type | Users | What it stresses |
|---|---|---|---|
| s01_browse.py | Read-only baseline | any | Buffer pool, HTTP serialisation |
| s02_grade.py | Write-heavy (grading) | any | InnoDB WAL flush, Hikari pool |
| s03_mixed.py | **Grid5000 reference** | 50 (60/40) | Everything — main experiment scenario |
| s04_import.py | Course import burst | 25 (80/20) | Bulk INSERTs, JVM heap, long transactions |

**S03 vs S04 distinction:**
- S03 — data is pre-seeded; workload operates on a fixed dataset (steady-state teaching ops)
- S04 — the import IS the workload; DB grows during the run (start-of-semester burst)

---

## What is done ✅

### Local development stack
- [x] `docker-compose.yml` with Prometheus, Grafana, node-exporter, cAdvisor, mysqld-exporter v0.14.0
- [x] All 5 Prometheus targets UP and verified
- [x] App metrics confirmed at `:9091/management/prometheus` (management port, not 8082)
- [x] DB seeded: 3× 357.json → 4 courses, 4 exams, 51 sheets, ~765 student responses
- [x] Known: `admin/admin` credentials required (data owned by admin user)

### Locust scenarios
- [x] `s01_browse.py` — smoke tested, 0 failures
- [x] `s02_grade.py` — smoke tested, 0 failures
- [x] `s03_mixed.py` — smoke tested, 0 failures; confirmed as Grid5000 reference workload
- [x] `s04_import.py` — smoke tested, 0 failures; export→import round-trip verified (290 KB, 101 ms)

### Local experiment results
- [x] Run `20260626_015650_local` — 5-min run, 50 users, s03_mixed, 0 failures
  - 6 664 total requests, ~22 req/s steady state
  - p50 = 9 ms, p95 = 33 ms, p99 = 100 ms
  - Auth endpoint: p50 = 100 ms (DB lookup on every login)
  - Write path (PUT grade): p50 = 14 ms, p95 = 34 ms
- [x] Results saved to `lab/results/20260626_015650_local_*`

### Slide assets (`lab/slides/`)
- [x] `00_summary_kpis.png` — 6-tile KPI banner
- [x] `01_throughput_over_time.png` — req/s + active users over time
- [x] `02_latency_over_time.png` — p50/p90/p95 over time with ramp-up shaded
- [x] `03_latency_by_endpoint.png` — p50/p90/p95 per endpoint, writes highlighted
- [x] `04_request_volume.png` — request count per endpoint by type
- [x] `05_read_write_auth_comparison.png` — latency profile by operation class
- [x] `s01_browse_diagram.png` — sequence diagram S01
- [x] `s02_grade_diagram.png` — sequence diagram S02
- [x] `s03_mixed_diagram.png` — sequence diagram S03 (parallel lanes)
- [x] `lab/slides/scenarios_diagrams.md` — Mermaid source + comparison table

### Grid5000 infrastructure
- [x] `grid5000/reserve.py` — EnOSlib node reservation
  - Rennes site added (`paravance` default cluster)
  - `--node <fqdn>` flag for hardware pinning across runs
  - Saves node info to `grid5000/.node_info.json`
- [x] `grid5000/terraform/` — libvirt provider, CoW disk overlays, cloud-init
- [x] `grid5000/vm-docker-compose.yml` — app stack for VMs (no monitoring — scraped from node)
- [x] `grid5000/experiments.yml` — 8-config experiment matrix
- [x] `grid5000/scripts/run_experiment.py` — full orchestrator (provision → deploy → seed → locust → destroy → save)
- [x] `grid5000/monitoring/` — Prometheus + Grafana on bare-metal node, file-based SD

### Ansible automation
- [x] `ansible/ansible.cfg` — SSH pipelining, ControlMaster, root user
- [x] `ansible/inventory/dynamic.py` — reads `.node_info.json`, generates inventory
- [x] `ansible/inventory/group_vars/all.yml` — shared vars (paths, registry URL, etc.)
- [x] `ansible/playbooks/setup_node.yml` — installs everything, builds images, starts monitoring
- [x] `ansible/playbooks/deploy_app.yml` — deploys app on VM via SSH from node
- [x] `ansible/playbooks/seed_db.yml` — seeds DB on VM (3× 357.json)
- [x] `ansible/playbooks/collect_results.yml` — pulls `lab/results/` to laptop
- [x] `run_experiment.py` updated to call `ansible-playbook` instead of bash scripts

### ARM image fix
- [x] `setup_node.yml` builds both images on the node (linux/amd64) and pushes to
  local registry at `192.168.122.1:5000`. VMs pull from there via `.env` override.
  DockerHub ARM images are no longer used on Grid5000.

---

## What is in progress 🔄

- [ ] **First Grid5000 run** — not yet executed. Blocked on: access to the platform,
  reserving a node, running `setup_node.yml` (includes 30–50 min image build).

---

## What remains to do ❌

### Grid5000 experiments
- [ ] Reserve a Rennes node (`python3 grid5000/reserve.py --site rennes`)
- [ ] Run `setup_node.yml` (first time: ~45 min for Maven + npm builds)
- [ ] Save the node FQDN for future reservations (`--node` pinning)
- [ ] Run Phase 1 — RAM sweep (5 configs, ~2 h)
- [ ] Run Phase 2 — CPU sweep (3 configs, ~1 h)
- [ ] Run S04 import scenario across a subset of configs (suggested: 2cpu-1gb, 2cpu-4gb, 8cpu-4gb)
- [ ] Pull results (`collect_results.yml`)

### Analysis
- [ ] Generate cross-config comparison graphs (throughput vs RAM, latency vs vCPU)
  — the `lab/slides/generate_graphs.py` only covers the local single-config run;
  a new script is needed to compare across the 8 experiment configs
- [ ] Identify breaking points per config and annotate on graphs
- [ ] Correlate JVM heap / GC metrics (Prometheus) with Locust latency spikes
- [ ] Document findings in `lab/METRICS.md` or a new `lab/RESULTS.md`

### Sequence diagram for S04
- [ ] Add `s04_import_diagram.png` to `lab/slides/` (S04 was added after the diagrams were generated)

### Known bugs (not blocking experiments)
- [ ] `testData/136.json` full import fails with silent HTTP 500 — root cause unconfirmed
  (suspected constraint violation in student data loop in `ExtendedAPI.importCourse`)
- [ ] `ImportExportService.java:638–644` — copy-paste bug: exam associations written
  to `finalResultStudentR` instead of `finalResultExamR` (affects export with student data;
  `importCourseWithoutStudentData` is unaffected and used in S04)

### Paper / thesis
- [ ] Write experiment section (methodology, configs, results)
- [ ] Formulate IaC patch recommendations grounded in the data
- [ ] Compare S03 vs S04 findings (what the thesis calls Stage 1 experiments)

---

## Key decisions log

| Decision | Rationale |
|---|---|
| `admin/admin` credentials in all scenarios | Data is owned by admin; `user` sees empty lists |
| mysqld-exporter v0.14.0 (not v0.15.1) | v0.15.1 dropped `DATA_SOURCE_NAME` env var support |
| `importCourseWithoutStudentData` in S04 | Avoids copy-paste bug in ImportExportService; structurally equivalent stress |
| Monitoring on bare-metal node, not in VMs | VMs are the variable being tested; monitoring must not consume their RAM/CPU |
| One VM at a time (sequential experiments) | Concurrent VMs on the same node would compete for resources and invalidate measurements |
| Local registry instead of DockerHub | Solves ARM cross-compilation; no rate limits; images guaranteed amd64 |
| Rennes as default site | Back online as of 2026-06; `paravance` cluster is modern (Xeon E5-2630 v3, 128 GB RAM) |
| Node pinning via `--node` flag | Hardware reproducibility — same CPU microarchitecture across all runs |
| S03 as the main Grid5000 scenario | Best approximation of daily platform load; clean basis for RAM vs CPU comparison |
| S04 as a separate experiment axis | Answers a different question: ingestion phase, not steady-state operation |

---

## Port reference

| Port | Service | Where |
|---|---|---|
| 8082 | Quarkus API | VM host |
| 9091 | Quarkus management/metrics | VM host |
| 8080 | Angular frontend | VM host |
| 3306 | MySQL | VM (internal) |
| 9000/9090 | Minio API / console | VM (internal) |
| 9100 | node-exporter | VM host |
| 8081 | cAdvisor | VM host |
| 9104 | mysqld-exporter | VM host |
| 3000 | Grafana | Bare-metal node |
| 9092 | Prometheus | Bare-metal node |
| 5000 | Local image registry | Bare-metal node |

---

## Full workflow (one-liner cheat sheet)

```bash
# 1 — Reserve (laptop)
python3 grid5000/reserve.py --site rennes --walltime 03:00:00

# 1b — Reuse same node
python3 grid5000/reserve.py --site rennes \
  --node $(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")

# 2 — Bootstrap node (laptop, ~45 min first time)
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/setup_node.yml

# 3 — Open monitoring tunnel (laptop, keep in background)
NODE=$(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")
ssh -N -L 3000:localhost:3000 -L 9092:localhost:9092 root@$NODE &

# 4 — Run all experiments (on the node, ~3 h)
ssh root@$NODE "cd /root/CorrigeExam && python3 grid5000/scripts/run_experiment.py --all"

# 5 — Collect results (laptop)
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/collect_results.yml
```
