#!/usr/bin/env python3
from __future__ import annotations
"""
run_experiment.py — Orchestrate Grid5000 provisioning experiments (parallel-capable).

Runs ON the bare-metal node (not from the laptop).

What it does per experiment config:
  1. Terraform apply  → provision a KVM VM (per-config state file, no conflicts)
  2. virsh vcpupin    → pin VM vCPUs to isolated host CPUs (parallel isolation)
  3. ansible deploy_app.yml  → copy docker-compose + seed files, start the app
  4. ansible seed_db.yml     → import test data so the DB is populated
  5. Update Prometheus targets (per-config files: vm_<label>_node.json, …)
  6. Locust           → run s03_mixed at increasing user counts until saturation
                        Retries transient failures up to --retry times
  7. Terraform destroy → clean up the VM, release CPU slot
  8. Save results to lab/results/<config>/<run_id>/

Usage:
  # Single config:
  python3 grid5000/scripts/run_experiment.py --config 2cpu-4gb

  # All configs, up to 3 in parallel:
  python3 grid5000/scripts/run_experiment.py --all --parallel 3

  # Phase 1 only, skip already-done configs:
  python3 grid5000/scripts/run_experiment.py --phase 1 --skip 2cpu-4gb --parallel 2

  # Dry-run:
  python3 grid5000/scripts/run_experiment.py --all --parallel 3 --dry-run

CPU isolation (parallel mode):
  Host CPUs 0–(LOCUST_CPU_END) are reserved for the OS, monitoring, and Locust.
  Host CPUs (VM_CPU_START)–(HOST_TOTAL_CPUS-1) are dynamically allocated to VMs.
  Each VM gets exactly vcpus host CPUs pinned via virsh vcpupin.
  Locust is pinned to the OS range via taskset.

Results layout:
  lab/results/<config>/<run_id>/
    u<N>_stats.csv
    u<N>_stats_history.csv
    u<N>_system.csv    ← VM metrics from Prometheus
    summary.json
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

# ── paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT   = Path(__file__).resolve().parents[2]
TF_DIR      = REPO_ROOT / "grid5000" / "terraform"
ANSIBLE_DIR = REPO_ROOT / "ansible"
INVENTORY   = ANSIBLE_DIR / "inventory" / "dynamic.py"
TARGETS_DIR = REPO_ROOT / "grid5000" / "monitoring" / "targets"
RESULTS_DIR = REPO_ROOT / "lab" / "results"
SCENARIOS   = REPO_ROOT / "lab" / "scenarios"
EXPERIMENTS = REPO_ROOT / "grid5000" / "experiments.yml"

PROMETHEUS_URL  = "http://localhost:9092"
SCRAPE_INTERVAL = 15  # seconds

# CPU layout on the bare-metal node
HOST_TOTAL_CPUS = 32   # parasilo nodes: 32 physical cores
VM_CPU_START    = 8    # CPUs 0-7 → OS + monitoring + Locust
LOCUST_CPU_MASK = "0-7"  # taskset argument for Locust processes

_sudo_user = os.environ.get("SUDO_USER")
SSH_KEY = (Path("/home") / _sudo_user if _sudo_user else Path.home()) / ".ssh" / "id_rsa"

# ── thread-safe logging ────────────────────────────────────────────────────────
_print_lock = threading.Lock()

def log(label: str, msg: str):
    with _print_lock:
        print(f"[{label}] {msg}", flush=True)


# ── CPU slot pool ──────────────────────────────────────────────────────────────

class CpuSlotPool:
    """
    Thread-safe allocator of non-overlapping host CPU ranges for VM pinning.

    The pool covers CPUs VM_CPU_START … HOST_TOTAL_CPUS-1.
    Each VM acquires 'vcpus' CPUs on provision and releases them on destroy.
    When parallel=1 (sequential mode) pinning is skipped entirely.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._free: list[int] = list(range(VM_CPU_START, HOST_TOTAL_CPUS))

    def acquire(self, n: int, label: str) -> list[int]:
        with self._lock:
            if len(self._free) < n:
                raise RuntimeError(
                    f"[{label}] CPU slot pool exhausted "
                    f"(need {n} CPUs, only {len(self._free)} left: {self._free}). "
                    f"Reduce --parallel or wait for a running experiment to finish."
                )
            slot = self._free[:n]
            self._free = self._free[n:]
            return slot

    def release(self, cpus: list[int]):
        with self._lock:
            self._free = sorted(self._free + cpus)

    def available(self) -> int:
        with self._lock:
            return len(self._free)


_cpu_pool = CpuSlotPool()


# ── helpers ────────────────────────────────────────────────────────────────────

def run(cmd: list[str], check=True, capture=False, label: str = "", **kw) -> subprocess.CompletedProcess:
    prefix = f"[{label}] " if label else ""
    with _print_lock:
        print(f"{prefix}  $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, **kw)


def load_experiments() -> dict:
    with open(EXPERIMENTS) as f:
        return yaml.safe_load(f)


def select_configs(conf: dict, label: str | None, phase: int | None,
                   skip: list[str] | None = None) -> list[dict]:
    all_configs = conf["experiments"]
    if label:
        matches = [c for c in all_configs if c["label"] == label]
        if not matches:
            labels = [c["label"] for c in all_configs]
            sys.exit(f"Config '{label}' not found. Available: {labels}")
        return matches
    if phase is not None:
        configs = [c for c in all_configs if c.get("phase") == phase]
    else:
        configs = all_configs
    if skip:
        configs = [c for c in configs if c["label"] not in skip]
    return configs


# ── Terraform ──────────────────────────────────────────────────────────────────

def tf(*args, label: str, capture=False) -> subprocess.CompletedProcess:
    """Run terraform with a per-config state file so parallel runs don't collide."""
    state_file = str(TF_DIR / f"{label}.tfstate")
    return run(
        ["terraform", f"-state={state_file}", *args],
        cwd=TF_DIR, capture=capture, label=label,
    )


def _virsh_cleanup(label: str):
    """Remove any stale libvirt domain and its volumes before a fresh apply."""
    subprocess.run(["virsh", "destroy",  label], capture_output=True)
    subprocess.run(["virsh", "undefine", label], capture_output=True)
    for suffix in ("-disk.qcow2", "-cloudinit.iso"):
        subprocess.run(
            ["virsh", "vol-delete", f"{label}{suffix}", "--pool", "default"],
            capture_output=True,
        )


def _pin_vm_cpus(label: str, vcpus: int, host_cpus: list[int]):
    """Pin each VM vCPU to an isolated host CPU via virsh vcpupin."""
    log(label, f"CPU pinning: vCPUs {list(range(vcpus))} → host CPUs {host_cpus}")
    for vcpu_id, host_cpu in enumerate(host_cpus[:vcpus]):
        result = subprocess.run(
            ["virsh", "vcpupin", label, str(vcpu_id), str(host_cpu)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(label, f"  vcpupin warning (vCPU {vcpu_id} → host {host_cpu}): {result.stderr.strip()}")


def provision_vm(config: dict, parallel: bool) -> tuple[str, list[int]]:
    """
    Apply Terraform for the given config.

    Returns (vm_ip, allocated_host_cpus).
    In parallel mode, acquires CPU slots from the pool and pins the VM.
    In sequential mode, returns an empty cpu list (no pinning needed).
    """
    label = config["label"]
    log(label, f"Provisioning VM ({config['vcpus']} vCPU, {config['ram_mb']} MB RAM) …")
    _virsh_cleanup(label)

    tf("apply", "-auto-approve", "-lock=false",
       f"-var=vm_name={label}",
       f"-var=vm_vcpus={config['vcpus']}",
       f"-var=vm_ram_mb={config['ram_mb']}",
       f"-var=vm_disk_gb={config.get('disk_gb', 20)}",
       f"-var=ssh_public_key_path={SSH_KEY}.pub",
       label=label,
    )
    result = tf("output", "-raw", "vm_ip", label=label, capture=True)
    vm_ip = result.stdout.strip()
    log(label, f"VM IP: {vm_ip}")

    host_cpus: list[int] = []
    if parallel:
        host_cpus = _cpu_pool.acquire(config["vcpus"], label)
        _pin_vm_cpus(label, config["vcpus"], host_cpus)

    return vm_ip, host_cpus


def destroy_vm(label: str, host_cpus: list[int]):
    log(label, "Destroying VM …")
    try:
        tf("destroy", "-auto-approve", "-lock=false",
           f"-var=vm_name={label}", label=label)
    finally:
        if host_cpus:
            _cpu_pool.release(host_cpus)
            log(label, f"Released host CPUs {host_cpus} back to pool "
                f"({_cpu_pool.available()} free)")


# ── Prometheus target management ───────────────────────────────────────────────
# Per-config target files: vm_<label>_node.json, vm_<label>_cadvisor.json, …
# prometheus.yml uses glob patterns (vm_*_node.json) to pick them all up.

def _target_files(label: str) -> dict[str, str]:
    return {
        "node":     f"vm_{label}_node.json",
        "cadvisor": f"vm_{label}_cadvisor.json",
        "app":      f"vm_{label}_app.json",
        "mysql":    f"vm_{label}_mysql.json",
    }


def update_prometheus_targets(config: dict, vm_ip: str):
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    label  = config["label"]
    labels = {
        "config": label,
        "vcpus":  str(config["vcpus"]),
        "ram_mb": str(config["ram_mb"]),
        "phase":  str(config.get("phase", "?")),
    }
    files = _target_files(label)
    entries = {
        files["node"]:     f"{vm_ip}:9100",
        files["cadvisor"]: f"{vm_ip}:8081",
        files["app"]:      f"{vm_ip}:9091",
        files["mysql"]:    f"{vm_ip}:9104",
    }
    for fname, target in entries.items():
        (TARGETS_DIR / fname).write_text(
            json.dumps([{"targets": [target], "labels": labels}], indent=2)
        )
    log(label, f"Prometheus targets written for {vm_ip}")
    _prometheus_reload()


def clear_prometheus_targets(label: str):
    """Reset this config's target files to [] so a destroyed VM stops being scraped."""
    for fname in _target_files(label).values():
        path = TARGETS_DIR / fname
        path.write_text("[]")


def _prometheus_reload():
    try:
        req = urllib.request.Request(f"{PROMETHEUS_URL}/-/reload", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        with _print_lock:
            print(f"[prometheus] Reload request failed (non-fatal): {exc}", flush=True)


# ── System metrics collection ──────────────────────────────────────────────────

_SYSTEM_METRICS: dict[str, str] = {
    # VM-level (node-exporter)
    "cpu_pct":
        "100 * (1 - avg(rate(node_cpu_seconds_total"
        "{{instance='{ip}:9100', mode='idle'}}[1m])))",
    "ram_used_bytes":
        "node_memory_MemTotal_bytes{{instance='{ip}:9100'}}"
        " - node_memory_MemAvailable_bytes{{instance='{ip}:9100'}}",
    "ram_total_bytes":
        "node_memory_MemTotal_bytes{{instance='{ip}:9100'}}",
    "swap_used_bytes":
        "node_memory_SwapTotal_bytes{{instance='{ip}:9100'}}"
        " - node_memory_SwapFree_bytes{{instance='{ip}:9100'}}",
    "swap_total_bytes":
        "node_memory_SwapTotal_bytes{{instance='{ip}:9100'}}",
    "disk_read_bps":
        "sum(rate(node_disk_read_bytes_total{{instance='{ip}:9100'}}[1m]))",
    "disk_write_bps":
        "sum(rate(node_disk_written_bytes_total{{instance='{ip}:9100'}}[1m]))",
    "net_rx_bps":
        "sum(rate(node_network_receive_bytes_total"
        "{{instance='{ip}:9100', device!='lo'}}[1m]))",
    "net_tx_bps":
        "sum(rate(node_network_transmit_bytes_total"
        "{{instance='{ip}:9100', device!='lo'}}[1m]))",
    # Container-level (cadvisor) — name=~ handles /name and name variants
    "mysql_cpu_pct":
        "100 * rate(container_cpu_usage_seconds_total"
        "{{instance='{ip}:8081', name=~'/?correctexam-mysql'}}[1m])",
    "mysql_mem_bytes":
        "container_memory_usage_bytes"
        "{{instance='{ip}:8081', name=~'/?correctexam-mysql'}}",
    "back_cpu_pct":
        "100 * rate(container_cpu_usage_seconds_total"
        "{{instance='{ip}:8081', name=~'/?correctexam-back'}}[1m])",
    "back_mem_bytes":
        "container_memory_usage_bytes"
        "{{instance='{ip}:8081', name=~'/?correctexam-back'}}",
    # JVM (Quarkus Micrometer)
    "jvm_heap_bytes":
        "sum(jvm_memory_used_bytes{{instance='{ip}:9091', area='heap'}})",
    "jvm_nonheap_bytes":
        "sum(jvm_memory_used_bytes{{instance='{ip}:9091', area='nonheap'}})",
    # MySQL (mysqld-exporter)
    "mysql_threads_connected":
        "mysql_global_status_threads_connected{{instance='{ip}:9104'}}",
    "mysql_queries_per_sec":
        "rate(mysql_global_status_queries{{instance='{ip}:9104'}}[1m])",
    "mysql_slow_queries":
        "rate(mysql_global_status_slow_queries{{instance='{ip}:9104'}}[1m])",
}


def _prom_range(query: str, start: float, end: float) -> list[tuple[float, float]]:
    params = urllib.parse.urlencode({
        "query": query,
        "start": str(int(start)),
        "end":   str(int(end)),
        "step":  str(SCRAPE_INTERVAL),
    })
    url = f"{PROMETHEUS_URL}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "success":
            return []
        results = data.get("data", {}).get("result", [])
        if not results:
            return []
        if len(results) == 1:
            return [(float(ts), float(v)) for ts, v in results[0]["values"]]
        acc: dict[float, float] = defaultdict(float)
        for series in results:
            for ts, v in series["values"]:
                acc[float(ts)] += float(v)
        return sorted(acc.items())
    except Exception:
        return []


def collect_system_metrics(vm_ip: str, start_ts: float, end_ts: float,
                           out_path: Path, label: str = ""):
    series: dict[str, dict[float, float]] = {}
    all_ts: set[float] = set()

    for name, tmpl in _SYSTEM_METRICS.items():
        pts = _prom_range(tmpl.format(ip=vm_ip), start_ts, end_ts)
        if pts:
            series[name] = dict(pts)
            all_ts.update(ts for ts, _ in pts)

    if not series:
        log(label, "No system metrics from Prometheus — skipping.")
        return

    sorted_ts = sorted(all_ts)
    columns   = list(_SYSTEM_METRICS.keys())
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp"] + columns)
        for ts in sorted_ts:
            w.writerow([ts] + [series.get(col, {}).get(ts, "") for col in columns])

    log(label, f"{len(sorted_ts)} rows × {len(series)} metrics → {out_path.name}")


# ── Locust ─────────────────────────────────────────────────────────────────────

def run_locust_step(
    vm_ip: str,
    config: dict,
    users: int,
    duration: str,
    ramp_rate: int,
    run_dir: Path,
    max_retries: int,
    parallel: bool,
) -> tuple[dict, float, float, bool]:
    """
    Run one Locust load step, retrying on transient failure.

    Returns (stats, t_start, t_end, hard_fail).
    hard_fail=True only when ALL attempts (1 + max_retries) produced a non-zero
    exit AND a CSV was produced (i.e. it looked like a real capacity issue every time).
    Raises RuntimeError if Locust fails to produce any output.
    """
    label      = config["label"]
    csv_prefix = str(run_dir / f"u{users}")

    # Pin Locust to OS CPUs in parallel mode so it doesn't compete with VMs.
    taskset_prefix = ["taskset", "-c", LOCUST_CPU_MASK] if parallel else []

    cmd = [
        *taskset_prefix,
        "locust",
        "-f", str(SCENARIOS / "s03_mixed.py"),
        "--host", f"http://{vm_ip}:8082",
        "--headless",
        "-u", str(users),
        "-r", str(ramp_rate),
        "--run-time", duration,
        "--csv", csv_prefix,
        "--html", f"{csv_prefix}.html",
        "--loglevel", "WARNING",
    ]

    stats_path = Path(csv_prefix + "_stats.csv")
    attempts   = 1 + max_retries

    for attempt in range(1, attempts + 1):
        log(label, f"Locust {users} users × {duration}"
            + (f" (retry {attempt - 1}/{max_retries})" if attempt > 1 else ""))
        t_start = time.time()
        result  = run(cmd, check=False, label=label)
        t_end   = time.time()

        hard_fail = result.returncode != 0

        if hard_fail and not stats_path.exists():
            if attempt < attempts:
                log(label, f"  Locust produced no output (exit {result.returncode}), "
                    f"retrying in 30 s …")
                time.sleep(30)
                continue
            raise RuntimeError(
                f"Locust failed to produce output after {attempts} attempt(s) "
                f"(exit {result.returncode})"
            )

        if hard_fail and attempt < attempts:
            stats = parse_stats(stats_path)
            log(label, f"  Locust exited {result.returncode} at {users} users "
                f"(failure_rate={stats.get('failure_rate', '?'):.2%}, "
                f"p95={stats.get('p95_ms', '?'):.0f} ms). "
                f"Retrying in 30 s …")
            time.sleep(30)
            # Remove the old CSV so the retry starts clean
            for suffix in ("_stats.csv", "_stats_history.csv",
                           "_failures.csv", "_exceptions.csv"):
                p = run_dir / f"u{users}{suffix}"
                if p.exists():
                    p.unlink()
            continue

        # Success or final attempt
        return parse_stats(stats_path), t_start, t_end, hard_fail

    # Should not reach here
    return parse_stats(stats_path), t_start, t_end, True  # type: ignore[return-value]


def parse_stats(stats_csv: Path) -> dict:
    if not stats_csv.exists():
        return {}
    with open(stats_csv) as f:
        for row in csv.DictReader(f):
            if row.get("Name") == "Aggregated":
                total    = int(row.get("Request Count", 0) or 0)
                failures = int(row.get("Failure Count", 0) or 0)
                p95      = float(row.get("95%", 0) or 0)
                return {
                    "total":        total,
                    "failures":     failures,
                    "failure_rate": failures / total if total else 0.0,
                    "p95_ms":       p95,
                    "rps":          float(row.get("Requests/s", 0) or 0),
                }
    return {}


def is_saturated(stats: dict, thresholds: dict) -> bool:
    if not stats:
        return False
    fail_threshold = thresholds["failure_rate_pct"] / 100.0
    p95_threshold  = thresholds["p95_ms"]
    sat = stats["failure_rate"] > fail_threshold or stats["p95_ms"] > p95_threshold
    if sat:
        with _print_lock:
            print(
                f"  SATURATION — "
                f"failure_rate={stats['failure_rate']:.1%}, "
                f"p95={stats['p95_ms']:.0f} ms",
                flush=True,
            )
    return sat


# ── Experiment runner ──────────────────────────────────────────────────────────

def run_one_experiment(config: dict, exp_conf: dict,
                       dry_run: bool = False,
                       max_retries: int = 2,
                       parallel: bool = False) -> dict:
    """
    Full cycle for one VM config:
      provision → pin CPUs → deploy → seed → load steps → destroy.

    Always returns a summary dict; errors are captured inside it.
    Destroys the VM and saves summary.json even if something fails mid-run.
    """
    label  = config["label"]
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / label / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = {"label": label, "run_id": run_id, "steps": []}

    if dry_run:
        log(label, f"[dry-run] Would run → {run_dir}")
        return summary

    vm_ip: str | None = None
    host_cpus: list[int] = []

    try:
        # 1 — Provision VM (+ CPU pinning in parallel mode)
        t0 = time.time()
        vm_ip, host_cpus = provision_vm(config, parallel)
        summary["provision_s"] = round(time.time() - t0)

        # 2 — Deploy app
        log(label, f"Deploying app on {vm_ip} …")
        run([
            "ansible-playbook",
            "-i", str(INVENTORY),
            str(ANSIBLE_DIR / "playbooks" / "deploy_app.yml"),
            "-e", f"vm_ip={vm_ip}",
            "--private-key", str(SSH_KEY),
        ], label=label)

        # 3 — Seed DB
        log(label, f"Seeding DB on {vm_ip} …")
        run([
            "ansible-playbook",
            "-i", str(INVENTORY),
            str(ANSIBLE_DIR / "playbooks" / "seed_db.yml"),
            "-e", f"vm_ip={vm_ip}",
            "--private-key", str(SSH_KEY),
        ], label=label)

        # 4 — Update Prometheus targets, wait one scrape cycle
        update_prometheus_targets(config, vm_ip)
        time.sleep(SCRAPE_INTERVAL + 5)

        # 5 — Load ramp
        thresholds     = exp_conf["load_steps"]["saturation"]
        breaking_point = None

        for users in exp_conf["load_steps"]["users"]:
            stats, t_start, t_end, hard_fail = run_locust_step(
                vm_ip=vm_ip,
                config=config,
                users=users,
                duration=exp_conf["load_steps"]["duration"],
                ramp_rate=exp_conf["load_steps"]["ramp_rate"],
                run_dir=run_dir,
                max_retries=max_retries,
                parallel=parallel,
            )

            collect_system_metrics(
                vm_ip=vm_ip,
                start_ts=t_start,
                end_ts=t_end,
                out_path=run_dir / f"u{users}_system.csv",
                label=label,
            )

            saturated = not hard_fail and is_saturated(stats, thresholds)
            status = (
                "locust_error" if hard_fail
                else "saturated"  if saturated
                else "ok"
            )
            summary["steps"].append({"users": users, "status": status, **stats})

            if hard_fail:
                summary["infra_failure_users"] = users
                log(label, f"Hard infra failure at {users} users after all retries "
                    f"(failure_rate={stats.get('failure_rate', '?'):.2%}, "
                    f"p95={stats.get('p95_ms', '?'):.0f} ms) — stopping ramp.")
                break

            if saturated:
                breaking_point = users
                break

        summary["breaking_point_users"] = breaking_point
        limit = (
            f"threshold saturation at {breaking_point} users" if breaking_point
            else f"hard infra failure at {summary.get('infra_failure_users')} users"
            if summary.get("infra_failure_users")
            else "no saturation up to max load"
        )
        log(label, f"Result: {limit}")

    except Exception as exc:
        summary["error"] = str(exc)
        log(label, f"Unexpected error: {exc}")

    finally:
        # 6 — Always destroy (releases CPU slot) and clear targets
        if vm_ip is not None:
            try:
                destroy_vm(label, host_cpus)
            except Exception as exc:
                log(label, f"destroy failed (manual cleanup needed): {exc}")
        elif host_cpus:
            _cpu_pool.release(host_cpus)

        clear_prometheus_targets(label)

        # 7 — Always save summary (partial data is valuable)
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, indent=2))
        log(label, f"Summary → {summary_path}")

    return summary


# ── Results table ──────────────────────────────────────────────────────────────

def print_comparison_table(summaries: list[dict]):
    if not summaries:
        return
    print("\n" + "=" * 90)
    print("EXPERIMENT RESULTS SUMMARY")
    print("=" * 90)
    print(
        f"{'Config':<16} {'Threshold sat.':>15} {'Infra failure':>14}"
        f" {'Max p95 (ms)':>13} {'Max fail%':>10}"
    )
    print("-" * 90)
    for s in summaries:
        steps    = s.get("steps", [])
        max_p95  = max((st.get("p95_ms",      0) for st in steps), default=0)
        max_fail = max((st.get("failure_rate", 0) for st in steps), default=0) * 100
        bp       = s.get("breaking_point_users")  or "—"
        infra    = s.get("infra_failure_users")    or "—"
        tag      = " [ERROR]" if s.get("error") else ""
        print(
            f"{s['label'] + tag:<16} {str(bp):>15} {str(infra):>14}"
            f" {max_p95:>13.0f} {max_fail:>9.1f}%"
        )
    print("=" * 90)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run Grid5000 provisioning experiments (parallel-capable)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", metavar="LABEL", help="Run a single experiment config")
    group.add_argument("--all",    action="store_true", help="Run all experiments")
    group.add_argument("--phase",  type=int, metavar="N", help="Run all Phase N experiments")

    parser.add_argument("--skip",     metavar="LABEL", action="append", default=[],
                        help="Skip a config label (repeatable)")
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help="Max concurrent experiments. >1 enables CPU pinning via virsh.")
    parser.add_argument("--retry",    type=int, default=2, metavar="N",
                        help="Max retries for a transient Locust step failure (per step).")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print plan without running anything")

    args     = parser.parse_args()
    exp_conf = load_experiments()
    configs  = select_configs(exp_conf, args.config, args.phase, skip=args.skip)
    parallel = args.parallel > 1

    print(f"Configs to run ({len(configs)}): {[c['label'] for c in configs]}")
    print(f"Parallelism: {args.parallel} | Retries per step: {args.retry}")
    if parallel:
        available_vm_cpus = HOST_TOTAL_CPUS - VM_CPU_START
        total_vcpus = sum(c["vcpus"] for c in configs)
        print(f"CPU pool: host CPUs {VM_CPU_START}–{HOST_TOTAL_CPUS - 1} "
              f"({available_vm_cpus} available), configs need {total_vcpus} vCPUs total")
        if total_vcpus > available_vm_cpus:
            print(f"WARNING: total vCPUs ({total_vcpus}) exceeds pool size "
                  f"({available_vm_cpus}). Experiments will queue for CPU slots.")
        print(f"Locust pinned to host CPUs {LOCUST_CPU_MASK} (OS + monitoring range)")

    if args.dry_run:
        print("[dry-run] No changes will be made.")

    summaries: list[dict] = []

    if args.parallel == 1:
        # Sequential — simple loop, no threads
        for config in configs:
            summary = run_one_experiment(
                config, exp_conf,
                dry_run=args.dry_run,
                max_retries=args.retry,
                parallel=False,
            )
            summaries.append(summary)
    else:
        # Parallel — ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(
                    run_one_experiment,
                    config, exp_conf,
                    args.dry_run, args.retry, True,
                ): config["label"]
                for config in configs
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    summaries.append(future.result())
                except Exception as exc:
                    print(f"[{label}] Unhandled exception: {exc}", flush=True)
                    summaries.append({"label": label, "error": str(exc), "steps": []})

    if not args.dry_run:
        print_comparison_table(summaries)


if __name__ == "__main__":
    main()
