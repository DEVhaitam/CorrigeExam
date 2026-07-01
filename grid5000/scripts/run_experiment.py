#!/usr/bin/env python3
from __future__ import annotations
"""
run_experiment.py — Orchestrate one or all Grid5000 provisioning experiments.

Runs ON the bare-metal node (not from the laptop).

What it does per experiment config:
  1. Terraform apply  → provision a KVM VM with the specified RAM/vCPU
  2. ansible deploy_app.yml  → copy docker-compose + seed files, start the app
  3. ansible seed_db.yml     → import test data so the DB is populated
  4. Update Prometheus targets to point at the VM IP
  5. Locust           → run s03_mixed at increasing user counts until saturation
  6. Terraform destroy → clean up the VM
  7. Save results to lab/results/<run_id>_<config>/

Usage:
  # Run a single experiment config:
  python3 grid5000/scripts/run_experiment.py --config 2cpu-4gb

  # Run ALL experiments from experiments.yml in order:
  python3 grid5000/scripts/run_experiment.py --all

  # Run only Phase 1 configs:
  python3 grid5000/scripts/run_experiment.py --phase 1

  # Dry-run — print what would happen without actually running:
  python3 grid5000/scripts/run_experiment.py --all --dry-run
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT    = Path(__file__).resolve().parents[2]
TF_DIR       = REPO_ROOT / "grid5000" / "terraform"
ANSIBLE_DIR  = REPO_ROOT / "ansible"
INVENTORY    = ANSIBLE_DIR / "inventory" / "dynamic.py"
TARGETS_DIR  = REPO_ROOT / "grid5000" / "monitoring" / "targets"
RESULTS_DIR  = REPO_ROOT / "lab" / "results"
SCENARIOS    = REPO_ROOT / "lab" / "scenarios"
EXPERIMENTS  = REPO_ROOT / "grid5000" / "experiments.yml"

PROMETHEUS_URL = "http://localhost:9092"
# When run via `sudo python3 ...`, Path.home() is /root but the key lives in the
# G5K user's NFS home. SUDO_USER is set by sudo to the original caller.
_sudo_user = os.environ.get("SUDO_USER")
SSH_KEY = (Path("/home") / _sudo_user if _sudo_user else Path.home()) / ".ssh" / "id_rsa"


# ── helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], check=True, capture=False, **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(
        cmd, check=check, capture_output=capture, text=True, **kw
    )


def load_experiments() -> dict:
    with open(EXPERIMENTS) as f:
        return yaml.safe_load(f)


def select_configs(conf: dict, label: str | None, phase: int | None) -> list[dict]:
    all_configs = conf["experiments"]
    if label:
        matches = [c for c in all_configs if c["label"] == label]
        if not matches:
            labels = [c["label"] for c in all_configs]
            sys.exit(f"Config '{label}' not found. Available: {labels}")
        return matches
    if phase is not None:
        return [c for c in all_configs if c.get("phase") == phase]
    return all_configs


# ── Terraform ────────────────────────────────────────────────────────────────

def tf(*args, capture=False) -> subprocess.CompletedProcess:
    return run(["terraform", *args], cwd=TF_DIR, capture=capture)


def _virsh_cleanup(label: str):
    """Remove any stale libvirt domain and its volumes before a fresh apply.

    Terraform tracks state separately from libvirt. If a previous apply failed
    after defining the domain (e.g. QEMU crashed, AppArmor blocked the image),
    the domain sits in libvirt as "shut off" but is absent from tfstate.  The
    next apply then fails with "domain already exists".  Pre-cleaning libvirt
    makes provision_vm idempotent across failures.
    """
    subprocess.run(["virsh", "destroy", label],  capture_output=True)
    subprocess.run(["virsh", "undefine", label], capture_output=True)
    for suffix in ("-disk.qcow2", "-cloudinit.iso"):
        subprocess.run(["virsh", "vol-delete", f"{label}{suffix}", "--pool", "default"],
                       capture_output=True)


def provision_vm(config: dict) -> str:
    """Apply Terraform for the given config, return the VM IP."""
    label = config["label"]
    print(f"\n[terraform] Provisioning VM: {label} "
          f"({config['vcpus']} vCPU, {config['ram_mb']} MB RAM) …")
    _virsh_cleanup(label)  # remove stale domain/volumes so apply starts clean
    tf("apply", "-auto-approve",
       f"-var=vm_name={label}",
       f"-var=vm_vcpus={config['vcpus']}",
       f"-var=vm_ram_mb={config['ram_mb']}",
       f"-var=vm_disk_gb={config.get('disk_gb', 20)}",
       f"-var=ssh_public_key_path={SSH_KEY}.pub",
    )
    result = tf("output", "-raw", "vm_ip", capture=True)
    vm_ip = result.stdout.strip()
    print(f"[terraform] VM IP: {vm_ip}")
    return vm_ip


def destroy_vm(label: str):
    print(f"\n[terraform] Destroying VM: {label} …")
    tf("destroy", "-auto-approve", f"-var=vm_name={label}")


# ── Prometheus target management ──────────────────────────────────────────────

def _write_target(filename: str, ip_port: str, labels: dict):
    entry = [{"targets": [ip_port], "labels": labels}]
    (TARGETS_DIR / filename).write_text(json.dumps(entry, indent=2))


def update_prometheus_targets(config: dict, vm_ip: str):
    """Write 4 target files (one per scrape endpoint) and reload Prometheus."""
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    labels = {
        "config": config["label"],
        "vcpus":  str(config["vcpus"]),
        "ram_mb": str(config["ram_mb"]),
        "phase":  str(config.get("phase", "?")),
    }
    # VM host port → Prometheus job mapping (from vm-docker-compose.yml)
    _write_target("vm_node.json",     f"{vm_ip}:9100", labels)
    _write_target("vm_cadvisor.json", f"{vm_ip}:8081", labels)
    _write_target("vm_app.json",      f"{vm_ip}:9091", labels)
    _write_target("vm_mysql.json",    f"{vm_ip}:9104", labels)
    print(f"[prometheus] 4 target files written → {vm_ip}  (config: {config['label']})")

    try:
        import urllib.request
        req = urllib.request.Request(
            f"{PROMETHEUS_URL}/-/reload", method="POST", data=b""
        )
        urllib.request.urlopen(req, timeout=5)
        print("[prometheus] Reload triggered")
    except Exception as exc:
        print(f"[prometheus] Reload request failed (non-fatal): {exc}")


def clear_prometheus_targets():
    """Reset all target files to empty so stale VMs don't appear in Prometheus."""
    for name in ("vm_node.json", "vm_cadvisor.json", "vm_app.json", "vm_mysql.json"):
        (TARGETS_DIR / name).write_text("[]")


# ── Locust ────────────────────────────────────────────────────────────────────

def run_locust_step(
    vm_ip: str,
    config: dict,
    users: int,
    duration: str,
    ramp_rate: int,
    run_prefix: Path,
) -> dict:
    """Run one locust step and return parsed stats."""
    csv_prefix = str(run_prefix) + f"_u{users}"
    cmd = [
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
    print(f"\n[locust] {config['label']} — {users} users × {duration}")
    run(cmd)
    return parse_stats(Path(csv_prefix + "_stats.csv"))


def parse_stats(stats_csv: Path) -> dict:
    """Return aggregated row from a locust stats CSV."""
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
        print(
            f"[locust] SATURATION DETECTED — "
            f"failure_rate={stats['failure_rate']:.1%}, "
            f"p95={stats['p95_ms']:.0f}ms"
        )
    return sat


# ── Experiment runner ─────────────────────────────────────────────────────────

def run_one_experiment(config: dict, exp_conf: dict, dry_run: bool = False) -> dict:
    """
    Full cycle for one VM config:
      provision → deploy → seed → load steps → destroy.
    Returns a summary dict.
    """
    label    = config["label"]
    run_id   = datetime.now().strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    run_prefix = RESULTS_DIR / f"{run_id}_{label}"
    summary    = {"label": label, "run_id": run_id, "steps": []}

    if dry_run:
        print(f"[dry-run] Would run experiment: {label}")
        return summary

    vm_ip = None
    try:
        # 1 — Provision VM
        t0 = time.time()
        vm_ip = provision_vm(config)
        summary["provision_s"] = round(time.time() - t0)

        # 2 — Deploy app
        print(f"\n[deploy] Deploying app on {vm_ip} …")
        run([
            "ansible-playbook",
            "-i", str(INVENTORY),
            str(ANSIBLE_DIR / "playbooks" / "deploy_app.yml"),
            "-e", f"vm_ip={vm_ip}",
            "--private-key", str(SSH_KEY),
        ])

        # 3 — Seed DB
        print(f"\n[seed] Seeding DB on {vm_ip} …")
        run([
            "ansible-playbook",
            "-i", str(INVENTORY),
            str(ANSIBLE_DIR / "playbooks" / "seed_db.yml"),
            "-e", f"vm_ip={vm_ip}",
            "--private-key", str(SSH_KEY),
        ])

        # 4 — Update Prometheus
        update_prometheus_targets(config, vm_ip)

        # 5 — Run load steps
        thresholds = exp_conf["load_steps"]["saturation"]
        breaking_point = None

        for users in exp_conf["load_steps"]["users"]:
            stats = run_locust_step(
                vm_ip=vm_ip,
                config=config,
                users=users,
                duration=exp_conf["load_steps"]["duration"],
                ramp_rate=exp_conf["load_steps"]["ramp_rate"],
                run_prefix=run_prefix,
            )
            summary["steps"].append({"users": users, **stats})

            if is_saturated(stats, thresholds):
                breaking_point = users
                break

        summary["breaking_point_users"] = breaking_point
        print(
            f"\n[result] {label}: "
            + (f"saturated at {breaking_point} users" if breaking_point else "no saturation up to max load")
        )

    finally:
        # 6 — Always destroy, even on error
        if vm_ip is not None:
            try:
                destroy_vm(label)
            except Exception as exc:
                print(f"[terraform] destroy failed (manual cleanup needed): {exc}")

        # Clear Prometheus targets so stale data doesn't confuse next run
        clear_prometheus_targets()

    # 7 — Save summary
    summary_file = RESULTS_DIR / f"{run_id}_{label}_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))
    print(f"[result] Summary → {summary_file}")

    return summary


def print_comparison_table(summaries: list[dict]):
    if not summaries:
        return
    print("\n" + "=" * 70)
    print("EXPERIMENT RESULTS SUMMARY")
    print("=" * 70)
    header = f"{'Config':<16} {'Break (users)':>14} {'Max p95 (ms)':>14} {'Max fail%':>10}"
    print(header)
    print("-" * 70)
    for s in summaries:
        steps = s.get("steps", [])
        max_p95  = max((st.get("p95_ms", 0) for st in steps), default=0)
        max_fail = max((st.get("failure_rate", 0) for st in steps), default=0) * 100
        bp       = s.get("breaking_point_users") or "> max"
        print(f"{s['label']:<16} {str(bp):>14} {max_p95:>14.0f} {max_fail:>9.1f}%")
    print("=" * 70)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run Grid5000 provisioning experiments",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config",   metavar="LABEL", help="Run a single experiment config")
    group.add_argument("--all",      action="store_true",  help="Run all experiments from experiments.yml")
    group.add_argument("--phase",    type=int, metavar="N", help="Run all Phase N experiments")
    parser.add_argument("--dry-run", action="store_true",  help="Print plan without running anything")

    args = parser.parse_args()

    exp_conf = load_experiments()
    configs  = select_configs(exp_conf, args.config, args.phase)

    print(f"Experiments to run ({len(configs)}): {[c['label'] for c in configs]}")
    if args.dry_run:
        print("[dry-run] No changes will be made.")

    summaries = []
    for config in configs:
        summary = run_one_experiment(config, exp_conf, dry_run=args.dry_run)
        summaries.append(summary)

    if not args.dry_run:
        print_comparison_table(summaries)


if __name__ == "__main__":
    main()
