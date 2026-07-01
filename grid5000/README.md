# Grid5000 — Provisioning-Level Performance Experiment

Stage 1 of the IaC performance study: vary **only** vCPU count and RAM at the
provisioning level, keep every other parameter fixed (same app config, same
docker-compose, same load scenario), and observe the impact on application
latency, throughput, and failure rate.

---

## Architecture

```
Your Laptop
│
│  (1) python3 grid5000/reserve.py   → reserves ONE bare-metal node
│  (6) SSH tunnel for Grafana
│
▼ SSH
Grid5000 Bare-Metal Node (reserved for the experiment duration)
│
├── Monitoring stack (Docker, runs directly on node — always up)
│   ├── Prometheus  :9092   — scrapes the active experiment VM
│   ├── Grafana     :3000   — dashboards (access via SSH tunnel from laptop)
│   └── node-exporter :9100 — metrics for the bare-metal node itself
│
├── Terraform (installed by setup_node.sh)
│   └── libvirt provider → manages KVM VMs on qemu:///system
│
└── Locust (installed by setup_node.sh)
    └── runs load tests against the active VM

    ┌─────────────────────────────────────────┐
    │  Experiment VM  (one at a time via KVM) │
    │  IP: 192.168.122.x (libvirt NAT)        │
    │                                         │
    │  docker-compose (app stack only):       │
    │  ├── correctexam-back   :8082           │
    │  ├── correctexam-front  :8080           │
    │  ├── mysql              :3306           │
    │  ├── minio              :9000           │
    │  ├── mysqld-exporter    :9104  ◄── Prometheus scrapes these
    │  ├── node-exporter      :9100  ◄──
    │  └── cadvisor           :8080  ◄──
    └─────────────────────────────────────────┘
```

**Why monitoring on the node (not in a VM)?**
- Grid5000 nodes have 32–128 GB RAM and 16–64 cores. Prometheus + Grafana
  need ~500 MB and < 1 core — negligible overhead vs. what the experiments need.
- Running monitoring in a dedicated VM wastes RAM that should go to the
  experiment VM.
- The node can reach all VMs directly at their libvirt NAT IPs (192.168.122.x).
- Access Grafana from your laptop via a single SSH port-forward.

**Why one VM at a time?**
Running multiple VMs simultaneously would create resource contention on the
shared node, making it impossible to isolate the effect of individual configs.
Each config gets the full node's I/O and cache bandwidth.

---

## Step-by-Step Workflow

### 0. Prerequisites (laptop)

```bash
pip install enoslib
pip install ansible        # if not already installed
```

---

### 1. Reserve a node

```bash
# From your laptop — uses EnOSlib to book a bare-metal node on Grid5000
python3 grid5000/reserve.py --site nancy --walltime 09:00:00
# Prints the node FQDN and saves it to grid5000/.node_info.json

# To reuse the same node across sessions (hardware pinning):
python3 grid5000/reserve.py --site nancy --walltime 09:00:00 \
  --node $(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")
```

> **Walltime guide** — Phase 1 only (5 configs): `05:00:00`  ·  Full matrix (8 configs): `08:00:00`

---

### 2. Bootstrap the node — Ansible (run from laptop, ~45 min first time)

The dynamic inventory reads `grid5000/.node_info.json` automatically — no manual host editing needed.

```bash
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/setup_node.yml
```

This playbook does everything in one shot:
- Installs Docker, KVM/libvirt, Terraform, Python, Locust
- Starts a local Docker registry on the node at `192.168.122.1:5000`
- Builds the backend and frontend images for **linux/amd64** directly on the node
  (avoids the ARM cross-compilation problem from an Apple Silicon laptop)
- Pushes the images to the local registry so VMs pull from there instead of DockerHub
- Initialises the Terraform libvirt provider
- Starts Prometheus + Grafana as Docker containers on the bare-metal node

```bash
# Force image rebuild after a code change:
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/setup_node.yml \
  -e rebuild_images=true
```

---

### 3. Open the Grafana tunnel (laptop — keep running in background)

```bash
NODE=$(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")
ssh -N -L 3000:localhost:3000 -L 9092:localhost:9092 root@$NODE &
# Grafana   → http://localhost:3000  (admin / admin)
# Prometheus → http://localhost:9092
```

---

### 4. Run the experiments (SSH into the node)

```bash
NODE=$(python3 -c "import json; print(json.load(open('grid5000/.node_info.json'))['node'])")
ssh root@$NODE
```

Once on the node:

```bash
cd /root/CorrigeExam

# Run ALL 8 configs sequentially (Phase 1 RAM sweep, then Phase 2 CPU sweep):
python3 grid5000/scripts/run_experiment.py --all

# Or run a single config:
python3 grid5000/scripts/run_experiment.py --config 2cpu-4gb
```

`run_experiment.py` handles the full lifecycle **automatically** for each config:

| Step | What happens |
|------|-------------|
| a | Terraform creates a KVM VM with the config's vCPU/RAM |
| b | `ansible-playbook deploy_app.yml` deploys the docker-compose stack on the VM |
| c | `ansible-playbook seed_db.yml` seeds the DB (3× 357.json — fixed dataset) |
| d | Locust runs load steps until saturation or all steps complete |
| e | CSVs saved to `lab/results/<timestamp>_<label>_u<N>_stats.csv` |
| f | Terraform destroys the VM before the next config |

**Manual playbooks** (for debugging or re-seeding without a full re-run):

```bash
# Re-deploy the app stack on a running VM:
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/deploy_app.yml \
  -e vm_ip=192.168.122.10

# Re-seed the DB on a running VM:
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/seed_db.yml \
  -e vm_ip=192.168.122.10
```

---

### 5. Collect results — Ansible (from laptop)

```bash
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/collect_results.yml
# Results land in lab/grid5000_results/<node>/

# Pull only a specific config's results:
ansible-playbook -i ansible/inventory/dynamic.py ansible/playbooks/collect_results.yml \
  -e run_filter=2cpu-4gb
```

---

## Experiment Matrix

Defined in `grid5000/experiments.yml`. Two phases:

| Phase | Variable | Fixed | Configs |
|-------|----------|-------|---------|
| 1 — RAM sweep  | RAM | 2 vCPUs | 512 MB, 1 GB, 2 GB, 4 GB, 8 GB |
| 2 — CPU sweep  | vCPU | 4 GB RAM | 1, 2, 4, 8 vCPUs |

Total: 8 experiments. Each runs s03_mixed at load steps
[10, 25, 50, 100, 200, 500, 1000, 2000, 4000] users × 5 min, stopping at saturation.

**Saturation criteria** (either condition):
- Failure rate > 5 %
- p95 latency > 30 000 ms

---

## Load-Step Strategy

For each VM config, `run_experiment.py` runs Locust in headless mode with
increasing user counts, stops as soon as saturation is detected, and records
the breaking-point user count.

```
Step 1:    10 users × 5 min  → parse stats → saturated? stop : continue
Step 2:    25 users × 5 min  → ...
Step 3:    50 users × 5 min  → ...
Step 4:   100 users × 5 min  → ...
Step 5:   200 users × 5 min  → ...
Step 6:   500 users × 5 min  → ...
Step 7: 1 000 users × 5 min  → ...
Step 8: 2 000 users × 5 min  → ...
Step 9: 4 000 users × 5 min  → done regardless
```

The CSV files from each step are labelled with the step's user count so you
can plot latency vs. load for each config and compare breaking points across
configs.

---

## Time Estimates

| Config stage | Per experiment | 5 configs | 8 configs |
|---|---|---|---|
| VM provision (cloud-init + Docker pull) | ~5 min | — | — |
| App deploy + seed (Ansible) | ~5 min | — | — |
| Load tests (9 steps × 5 min, stops at saturation) | ~10–45 min | — | — |
| VM destroy + cleanup | ~1 min | — | — |
| **Total per experiment (worst-case)** | **~56 min** | **~280 min (~5 h)** | **~450 min (~8 h)** |

> Constrained configs (512 MB, 1 GB) typically saturate at step 2–4, cutting their test
> time to 10–20 min. The worst-case only applies to well-provisioned configs that run all 9 steps.

→ Phase 1 only (5 configs) → reserve **`05:00:00`**  
→ Full matrix (8 configs)  → reserve **`08:00:00`**

---

## Monitoring Targets

The bare-metal node's Prometheus scrapes these ports on the active VM IP:

| Port | Exporter | Metrics |
|------|----------|---------|
| 9100 | node-exporter | VM CPU/RAM/disk/network |
| 8080 | cAdvisor | Per-container CPU/RAM |
| 9104 | mysqld-exporter | InnoDB, queries, connections |
| 9000 | correctexam-back (management) | JVM heap, GC, HikariCP, HTTP |

The experiment runner updates `/etc/prometheus/targets/*.json` before each
run and reloads Prometheus via `curl -X POST http://localhost:9092/-/reload`.

---

## Files Reference

```
grid5000/
├── README.md                   ← this file
├── reserve.py                  ← Step 1: EnOSlib node reservation → writes .node_info.json
├── experiments.yml             ← VM config matrix (load steps, saturation thresholds)
│
├── terraform/
│   ├── main.tf                 ← libvirt VM definition (parameterised RAM/vCPU)
│   ├── variables.tf
│   ├── outputs.tf
│   └── cloud-init/
│       └── user-data.yaml.tpl  ← Docker install + SSH key injection
│
├── monitoring/
│   ├── docker-compose.mon.yml  ← Prometheus + Grafana on the bare-metal node
│   ├── prometheus.yml          ← scrape config with file_sd for dynamic VM targets
│   └── targets/
│       └── vm.json.example     ← target file format (updated by run_experiment.py)
│
└── scripts/
    └── run_experiment.py       ← orchestration: provision → deploy → seed → locust → destroy

ansible/
├── ansible.cfg                 ← SSH pipelining, ControlMaster, root user
├── inventory/
│   ├── dynamic.py              ← reads .node_info.json, generates inventory automatically
│   └── group_vars/all.yml      ← shared vars (repo path, registry URL, VM SSH key, etc.)
└── playbooks/
    ├── setup_node.yml          ← Step 2: install everything + build images + start monitoring
    ├── deploy_app.yml          ← deploy docker-compose stack on a VM (called by run_experiment.py)
    ├── seed_db.yml             ← seed the DB on a VM (called by run_experiment.py)
    └── collect_results.yml     ← Step 5: pull lab/results/ from node to laptop
```
