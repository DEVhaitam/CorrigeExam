# Stage 1 — local docker-compose research deployment (S0)

Layered compose deployment for running misconfiguration scenarios locally
against the upstream CorrectExam stack. This is your S0 environment for
iterating before promoting experiments to Grid'5000 (S1+).

## How the layering works

Three concerns, three files, each with one job:

```
docker-compose.yml                  ← upstream CorrectExam, unmodified
iac/stage1-compose/
  research-overrides.yml            ← adds knobs, exporters, metric labels
  prometheus.yml                    ← scrape config (replaces upstream)
  scenarios/
    S0-baseline.env                 ← control: known-good values
    S1-heap-256m.env                ← L1 perturbation
    S4-mysql-buffer-128m.env        ← L3 perturbation
    S5-mysql-conns-30.env           ← L3 perturbation
    S6-quarkus-pool-5.env           ← L3 perturbation (cross-layer trap pair)
    S7-cpu-cap-half.env             ← L4 perturbation
    S8-mem-cap-512m.env             ← L4 perturbation
  run_scenario.sh                   ← convenience wrapper
```

Per scenario, you change ONE knob; everything else stays at baseline values.

## Running

```bash
chmod +x iac/stage1-compose/run_scenario.sh

# Baseline
./iac/stage1-compose/run_scenario.sh S0-baseline
# ... drive workload, snapshot metrics ...
./iac/stage1-compose/run_scenario.sh S0-baseline down

# Misconfig scenario
./iac/stage1-compose/run_scenario.sh S1-heap-256m
# ... drive workload, snapshot metrics ...
./iac/stage1-compose/run_scenario.sh S1-heap-256m down
```

`run_scenario.sh` uses a per-scenario `--project-name` so containers
from different scenarios don't collide if you accidentally leave one up.

## Endpoints once the stack is up

| Service | URL | Notes |
|---|---|---|
| Front (UI) | http://localhost:8080 | nginx serving Angular |
| Back (API) | http://localhost:8082 | Quarkus |
| Quarkus metrics | http://localhost:8082/q/metrics | confirm with curl |
| Prometheus | http://localhost:9092 | targets at /targets |
| Grafana | http://localhost:3000 | admin / admin |
| mysqld_exporter | http://localhost:9104/metrics | confirm with curl |
| MinIO console | http://localhost:9090 | admin / minioadmin |

## What to verify on first deployment

Before trusting any scenario data, confirm these in order:

1. **All services healthy.** `docker compose ... ps` — every service
   should show `healthy` or `running`. Pay attention to `back` —
   liquibase migrations on first start can take 30-60s.

2. **Quarkus exposes metrics.**
   ```bash
   curl -s http://localhost:8082/q/metrics | head -20
   ```
   Should return Prometheus-format lines. If 404, check the actual path
   in CorrectExam's `application.properties` — could be /management/prometheus
   on older Quarkus.

3. **Prometheus is scraping everything.** Visit http://localhost:9092/targets
   — every job should show "UP" except possibly `node` on macOS (which
   reports VM not host metrics, but should still be UP).

4. **mysqld_exporter is reporting metrics.**
   ```bash
   curl -s http://localhost:9104/metrics | grep mysql_global_status_threads_connected
   ```
   Should return a number.

5. **agroal_* metrics are available** (Quarkus DB pool — needed for S6):
   ```bash
   curl -s http://localhost:8082/q/metrics | grep agroal_
   ```
   Should show several lines. If empty, Quarkus may need
   `quarkus.datasource.metrics.enabled=true`.

If any of these fail, fix before running scenarios. A scenario run with
broken instrumentation produces garbage data.

## What's reliable on macOS / what's not

You're on Docker Desktop, which runs Linux in a VM. Implications:

| Trustworthy | Unreliable on macOS, reliable on Grid'5000 Linux |
|---|---|
| Quarkus Micrometer metrics | node_exporter (CPU, RAM, disk I/O of host) |
| mysqld_exporter | Per-CPU saturation metrics (S9 cpuset scenario) |
| MinIO Prometheus metrics | Disk I/O latency at host level |
| cAdvisor (container metrics) | Network bandwidth at host level |
| Locust workload-side measurements | |

The 6 scenarios in `scenarios/` were chosen because each can be diagnosed
from trustworthy metrics on macOS. S9 (cpuset-pin-conflict) was
deliberately excluded — it requires reliable per-core host metrics and
cannot be validated on macOS S0; defer to Grid'5000.

## Promoting to Grid'5000 (S1+)

When you're ready, the `.env` scenario files transfer unchanged. The
docker-compose deployment becomes EnOSlib-driven (already scaffolded in
the original deploy_s1.py). Per-node overrides are added at the EnOSlib
level. Nothing in this directory needs to change.
