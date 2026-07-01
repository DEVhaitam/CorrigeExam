# targets/

This directory is managed by `run_experiment.py`. Do not edit files here manually.

Before each experiment, the runner writes 4 JSON files with the current VM's IP:

| File | Endpoint | Prometheus job |
|------|----------|----------------|
| `vm_node.json`     | `<vm-ip>:9100`  | `vm-node-exporter` |
| `vm_cadvisor.json` | `<vm-ip>:8081`  | `vm-cadvisor` |
| `vm_app.json`      | `<vm-ip>:9091`  | `vm-correctexam-backend` |
| `vm_mysql.json`    | `<vm-ip>:9104`  | `vm-mysqld-exporter` |

Each file has the format:

```json
[{
  "targets": ["192.168.122.10:9100"],
  "labels": {
    "config":  "2cpu-4gb",
    "vcpus":   "2",
    "ram_mb":  "4096",
    "phase":   "1"
  }
}]
```

The `config` label appears in Grafana as a dimension, so you can compare
experiments side by side without restarting Prometheus or Grafana.

After each experiment, all files are reset to `[]` to stop scraping the
destroyed VM.

`setup_node.sh` initialises all 4 files to `[]` on first run.
