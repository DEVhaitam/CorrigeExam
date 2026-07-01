# Monitoring Guide — What to Watch and Why

The goal is not to collect every metric — it is to know which metric tells you
which config parameter is the bottleneck. This guide is organized by layer, then
by "what does a bad reading look like" for each metric.

---

## Layer 1 — Application (Quarkus / JVM)

**Source:** Micrometer/Prometheus at `http://localhost:9091/management/prometheus`
**Prometheus job:** `correctexam-backend`

### HTTP traffic

| Metric | PromQL | What to watch for |
|--------|--------|-------------------|
| Request rate | `rate(http_server_requests_seconds_count[1m])` | Flat rate under growing users → saturated |
| p50 latency | `histogram_quantile(0.50, rate(http_server_requests_seconds_bucket[1m]))` | Should stay < 200ms for reads |
| p95 latency | `histogram_quantile(0.95, rate(http_server_requests_seconds_bucket[1m]))` | Key SLO indicator |
| p99 latency | `histogram_quantile(0.99, rate(http_server_requests_seconds_bucket[1m]))` | Spikes here = GC pause or lock wait |
| Error rate | `rate(http_server_requests_seconds_count{outcome="SERVER_ERROR"}[1m])` | Should be 0; spikes = resource exhaustion |

**Label by endpoint:** add `{uri=~"/api/exams.*"}` etc. to find slow endpoints.

### JVM Memory & GC

The most impactful IaC parameter for a Quarkus app is `-Xmx` (heap ceiling).
Watching heap and GC lets you see the direct effect of changing it.

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Heap used | `jvm_memory_used_bytes{area="heap"}` | Rising towards max → GC pressure |
| Heap max | `jvm_memory_max_bytes{area="heap"}` | Ceiling (set by -Xmx) |
| Heap utilization | `jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"}` | > 80% sustained → undersized heap |
| GC pause rate | `rate(jvm_gc_pause_seconds_count[1m])` | Spike = app stopped-the-world |
| GC pause time | `rate(jvm_gc_pause_seconds_sum[1m])` | Time lost to GC per second; > 10% of wall time is bad |
| GC overhead % | `rate(jvm_gc_pause_seconds_sum[1m]) / 1 * 100` | Correlate with p99 latency spikes |

**Reading the signal:**
- Heap at 90%+ AND p99 spikes AND high GC pause rate → `-Xmx` too small
- Heap at 30% AND high latency → heap is NOT the bottleneck, look elsewhere

### Threads

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Live threads | `jvm_threads_live_threads` | Growing without bound = thread leak |
| Blocked threads | `jvm_threads_states_threads{state="blocked"}` | High = lock contention in app |
| Vert.x worker pool (active) | `worker_pool_active{pool_name="vert.x-worker-thread"}` | Saturated = increase worker pool size |
| Vert.x worker pool (queue) | `worker_pool_queue_size{pool_name="vert.x-worker-thread"}` | Non-zero = requests queuing for threads |

### DB Connection Pool (HikariCP)

Quarkus uses HikariCP by default. Pool starvation is a common bottleneck.

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Active connections | `hikaricp_connections_active` | Connections in use right now |
| Pending acquisition | `hikaricp_connections_pending` | Requests waiting for a connection |
| Pool max size | `hikaricp_connections_max` | Your configured ceiling |
| Connection timeout rate | `rate(hikaricp_connections_timeout_total[1m])` | Timeouts = pool too small |
| Acquisition time p95 | `histogram_quantile(0.95, rate(hikaricp_connections_acquire_seconds_bucket[1m]))` | Should be < 50ms |

**Reading the signal:**
- `pending > 0` sustained → pool too small, increase `quarkus.datasource.jdbc.max-size`
- `active == max` → you've hit the ceiling; either pool or DB can't absorb more

### JVM CPU

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| JVM process CPU | `process_cpu_usage` (0–1) | > 0.85 sustained = CPU-bound, consider more cores or lower GC overhead |
| System CPU | `system_cpu_usage` (0–1) | Baseline for comparing across VMs with different CPU limits |

---

## Layer 2 — Database (MySQL)

**Source:** mysqld-exporter at `correctexam-mysqld-exporter:9104`
**Prometheus job:** `mysqld-exporter`

This layer is critical for the IaC experiment because `innodb_buffer_pool_size`
and `max_connections` are the main MySQL config levers.

### Query throughput

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Queries per second | `rate(mysql_global_status_queries[1m])` | Total QPS; correlate with locust request rate |
| Select QPS | `rate(mysql_global_status_com_select[1m])` | Read load |
| Insert/Update/Delete QPS | `rate(mysql_global_status_com_insert[1m]) + rate(mysql_global_status_com_update[1m]) + rate(mysql_global_status_com_delete[1m])` | Write load |
| Slow queries | `rate(mysql_global_status_slow_queries[1m])` | Non-zero = missing index or buffer pool too small |

### InnoDB Buffer Pool

The buffer pool is MySQL's in-memory page cache. If it's too small, every read
hits disk instead of RAM. Directly controlled by `innodb_buffer_pool_size`.

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Buffer pool size | `mysql_global_variables_innodb_buffer_pool_size` | Config value (bytes) |
| Read requests | `rate(mysql_global_status_innodb_buffer_pool_read_requests[1m])` | Total logical reads |
| Disk reads | `rate(mysql_global_status_innodb_buffer_pool_reads[1m])` | Reads that went to disk |
| Buffer pool hit rate | `1 - rate(mysql_global_status_innodb_buffer_pool_reads[1m]) / rate(mysql_global_status_innodb_buffer_pool_read_requests[1m])` | **< 95% = buffer pool too small** |
| Dirty pages | `mysql_global_status_innodb_buffer_pool_pages_dirty` | High = flusher can't keep up with writes |

**This is one of the clearest IaC signals:** reduce `innodb_buffer_pool_size`
and watch the hit rate drop and latency rise — directly visible.

### Connections & Locking

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Current connections | `mysql_global_status_threads_connected` | Active client connections |
| Max connections configured | `mysql_global_variables_max_connections` | Ceiling |
| Connection utilization | `mysql_global_status_threads_connected / mysql_global_variables_max_connections` | > 80% = risk of "too many connections" |
| Table lock waits | `rate(mysql_global_status_table_locks_waited[1m])` | Row-level bypass needed |
| InnoDB row lock waits | `rate(mysql_global_status_innodb_row_lock_waits[1m])` | Concurrent writes fighting each other |
| InnoDB row lock time avg | `rate(mysql_global_status_innodb_row_lock_time[1m]) / rate(mysql_global_status_innodb_row_lock_waits[1m])` | Lock wait duration; > 50ms is a problem |
| Aborted connections | `rate(mysql_global_status_aborted_connects[1m])` | Connection failures (pool exhaustion or auth) |

### Disk / InnoDB I/O

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| InnoDB data reads | `rate(mysql_global_status_innodb_data_reads[1m])` | Physical disk read rate |
| InnoDB data writes | `rate(mysql_global_status_innodb_data_writes[1m])` | Physical disk write rate |
| InnoDB log writes | `rate(mysql_global_status_innodb_log_writes[1m])` | WAL/redo log write rate |
| InnoDB fsync calls | `rate(mysql_global_status_innodb_data_fsyncs[1m])` | High = disk is the bottleneck |

---

## Layer 3 — Containers (cAdvisor)

**Source:** cAdvisor
**Prometheus job:** `cadvisor`

Useful for seeing what each container actually consumes, and for verifying
that docker/cgroup resource limits are actually being enforced.

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| Container CPU usage | `rate(container_cpu_usage_seconds_total{name="correctexam-back"}[1m])` | Actual CPU cores consumed |
| CPU throttling | `rate(container_cpu_throttled_seconds_total{name="correctexam-back"}[1m])` | **Non-zero = CPU limit is the bottleneck** |
| Container memory usage | `container_memory_usage_bytes{name="correctexam-back"}` | RSS + cache |
| Container memory working set | `container_memory_working_set_bytes{name="correctexam-back"}` | What OOM killer uses; compare to limit |
| Network receive | `rate(container_network_receive_bytes_total{name="correctexam-back"}[1m])` | Inbound traffic |
| Network transmit | `rate(container_network_transmit_bytes_total{name="correctexam-back"}[1m])` | Outbound traffic (large for PDF uploads) |
| Filesystem reads | `rate(container_fs_reads_bytes_total{name="correctexam-back"}[1m])` | |
| Filesystem writes | `rate(container_fs_writes_bytes_total{name="correctexam-back"}[1m])` | High during PDF upload to Minio |

**Key insight for IaC experiments:**
Set a container CPU limit (e.g., `cpus: "1.0"`) in docker-compose and watch
`container_cpu_throttled_seconds_total` — when it's non-zero, you know the CPU
limit is the active constraint, not heap or DB pool.

---

## Layer 4 — System (node-exporter)

**Source:** node-exporter
**Prometheus job:** `node-exporter`

On local Docker Desktop (macOS), node-exporter sees the Docker VM, not the Mac.
On Linux bare metal (Grid5000), it sees the actual host.

| Metric | PromQL | What it tells you |
|--------|--------|-------------------|
| CPU utilization | `1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))` | Overall host CPU load |
| Per-core CPU | `rate(node_cpu_seconds_total{mode!="idle"}[1m])` | Uneven load across cores |
| Available memory | `node_memory_MemAvailable_bytes` | Drop close to 0 = swap pressure imminent |
| Swap used | `node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes` | Non-zero = OS under memory pressure |
| Disk read throughput | `rate(node_disk_read_bytes_total[1m])` | |
| Disk write throughput | `rate(node_disk_written_bytes_total[1m])` | |
| Disk await (latency) | `rate(node_disk_read_time_seconds_total[1m]) / rate(node_disk_reads_completed_total[1m])` | > 10ms = disk is a bottleneck |
| Network in | `rate(node_network_receive_bytes_total{device!="lo"}[1m])` | |
| Network out | `rate(node_network_transmit_bytes_total{device!="lo"}[1m])` | |
| Context switches | `rate(node_context_switches_total[1m])` | Very high = OS scheduler overloaded |
| Load average | `node_load1` vs number of cores | > 1.0 × cores = CPU saturation |

---

## Layer 5 — Load Generator (Locust)

Locust's own stats are as important as the app's metrics — they tell you what the
client experience actually is.

Locust outputs CSV files (when run with `--csv`) containing:
- `*_stats.csv` — requests/sec, median/p95/p99 response time, failure rate per endpoint
- `*_stats_history.csv` — same metrics over time (use for time-series plots)

| Thing to watch | Where to see it |
|----------------|-----------------|
| Requests/s | Locust web UI → Statistics tab, or `*_stats_history.csv` |
| Response time p95 / p99 | Same |
| Failure rate | Should be 0%; any % = app is rejecting requests |
| Users spawned vs target | If locust can't spawn requested users → locust machine is the bottleneck |

**Matching locust events to Grafana:**
Run locust with timestamps visible and use Grafana's "Add annotation" to mark
when you changed user count. This makes before/after comparisons clean.

---

## What Correlates with What

This is the key reference for interpreting results:

| Symptom in Grafana | Likely config bottleneck | Config parameter to tune |
|--------------------|--------------------------|--------------------------|
| p99 latency spikes every N seconds | GC pause (JVM heap full) | `-Xmx` / `-Xms` (heap size) |
| Latency rises, CPU flat | DB connection pool exhaustion | `quarkus.datasource.jdbc.max-size` |
| MySQL slow queries non-zero | Buffer pool miss (disk reads) | `innodb_buffer_pool_size` |
| CPU throttled non-zero | Container CPU limit hit | `cpus:` in docker-compose / VM vCPU count |
| High InnoDB row lock waits | Concurrent writes to same rows | App-level (sharding/batching) |
| Network saturation | PDF upload bandwidth | VM NIC throughput / MTU |
| Heap steady, GC low, latency high | Threads blocked (lock contention) | `quarkus.thread-pool.max-threads` |

---

## Grid5000 Monitoring Architecture

### Overview

```
Grid5000 bare metal node
├── VM1: app config A  (node-exporter:9100, cadvisor:8080, mysqld-exporter:9104, app:8080)
├── VM2: app config B  (node-exporter:9100, cadvisor:8080, mysqld-exporter:9104, app:8080)
├── VM3: app config C  (node-exporter:9100, cadvisor:8080, mysqld-exporter:9104, app:8080)
└── VM_mon: Prometheus + Grafana  ← scrapes all VMs + bare metal node-exporter
```

**Why this layout:** Each experiment VM only runs the app + lightweight exporters
(no Prometheus/Grafana overhead skewing results). One monitoring VM centralizes
everything so you can compare all configs side by side in a single Grafana dashboard.

Alternatively, if you have a dedicated bare metal node for monitoring, run Prometheus
and Grafana there and skip the monitoring VM.

### Prometheus config for multi-VM scraping

On the monitoring VM, `prometheus.yml` uses labels to tag each VM by config:

```yaml
global:
  scrape_interval: 15s
  external_labels:
    experiment_id: 'exp-001'   # set per experiment run

scrape_configs:
  # App metrics per VM
  - job_name: 'app'
    static_configs:
      - targets: ['vm1:8080']
        labels: { config: 'heap-512m', vm_id: 'vm1' }
      - targets: ['vm2:8080']
        labels: { config: 'heap-1g',   vm_id: 'vm2' }
      - targets: ['vm3:8080']
        labels: { config: 'heap-2g',   vm_id: 'vm3' }
    metrics_path: /management/prometheus

  # System metrics per VM
  - job_name: 'node'
    static_configs:
      - targets: ['vm1:9100']
        labels: { config: 'heap-512m', vm_id: 'vm1' }
      - targets: ['vm2:9100']
        labels: { config: 'heap-1g',   vm_id: 'vm2' }
      - targets: ['vm3:9100']
        labels: { config: 'heap-2g',   vm_id: 'vm3' }

  # MySQL metrics per VM
  - job_name: 'mysql'
    static_configs:
      - targets: ['vm1:9104']
        labels: { config: 'heap-512m', vm_id: 'vm1' }
      - targets: ['vm2:9104']
        labels: { config: 'heap-1g',   vm_id: 'vm2' }
      - targets: ['vm3:9104']
        labels: { config: 'heap-2g',   vm_id: 'vm3' }

  # Bare metal host (to see hypervisor-level resource contention between VMs)
  - job_name: 'baremetal'
    static_configs:
      - targets: ['baremetal-host:9100']
        labels: { role: 'hypervisor' }
```

### Why also scrape the bare metal host

If all VMs share the same physical cores and one VM's config causes heavy CPU usage,
it will starve the others — a hypervisor-level effect invisible from inside each VM.
The bare metal node-exporter exposes this: watch `node_load1` on the hypervisor
alongside per-VM `process_cpu_usage` to see if results are contaminated by
resource contention between VMs.

### Grafana dashboard for multi-config comparison

Key panels to build for the comparison dashboard:

1. **p95 latency by config** — `histogram_quantile(0.95, sum by (config, le) (rate(http_server_requests_seconds_bucket[1m])))`
2. **Request rate by config** — `sum by (config) (rate(http_server_requests_seconds_count[1m]))`
3. **Heap utilization by config** — `jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"}`
4. **GC time fraction by config** — `rate(jvm_gc_pause_seconds_sum[1m])`
5. **MySQL buffer pool hit rate by config** — `1 - rate(mysql_global_status_innodb_buffer_pool_reads[1m]) / rate(mysql_global_status_innodb_buffer_pool_read_requests[1m])`
6. **DB connection pending by config** — `hikaricp_connections_pending`
7. **CPU throttling by config** — `rate(container_cpu_throttled_seconds_total[1m])`

Use the `config` label as a variable in Grafana ($config) to filter or compare.

### What to deploy on each experiment VM

Minimum set of services per VM (add to the app's docker-compose):

```yaml
  node-exporter:
    image: prom/node-exporter:v1.8.0
    network_mode: host   # needed for accurate system metrics on Linux VMs
    pid: host
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
    command:
      - '--path.procfs=/host/proc'
      - '--path.sysfs=/host/sys'

  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.49.1
    privileged: true
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/docker:/var/lib/docker:ro

  mysqld-exporter:
    image: prom/mysqld-exporter:v0.15.1
    environment:
      DATA_SOURCE_NAME: "root:rootpassword@(mysql:3306)/"
```

No Prometheus or Grafana needed on experiment VMs — they just expose metrics,
the central monitoring VM collects them.

---

## Checklist Before Starting a Test Run

- [ ] Grafana is accessible and Prometheus datasource shows green
- [ ] All Prometheus targets are UP: http://localhost:9092/targets
- [ ] `mysqld-exporter` target is UP (check `mysql_up` = 1)
- [ ] DB is seeded (at minimum `136.json`)
- [ ] Auth works: `curl -s -X POST http://localhost:8082/api/authenticate -H "Content-Type: application/json" -d '{"username":"admin","password":"admin","rememberMe":false}'` returns a token
- [ ] Locust can reach the app: `curl http://localhost:8082/management/health`
- [ ] Note the time before starting locust (for Grafana time range alignment)
