#!/usr/bin/env bash
# seed_vm.sh — Seed the DB on an experiment VM with 3× testData/357.json.
#
# Runs on the bare-metal node.  Seeds via SSH so seed.py calls localhost:8082
# inside the VM (no --host flag needed on seed.py).
#
# Usage:
#   bash grid5000/scripts/seed_vm.sh <vm_ip> <ssh_key_path>
set -euo pipefail

VM_IP="${1:?Usage: seed_vm.sh <vm_ip> [ssh_key_path]}"
SSH_KEY="${2:-${HOME}/.ssh/id_rsa}"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30 -i ${SSH_KEY}"

echo "[seed] Seeding DB on ${VM_IP} with 3× 357.json …"

# Install requests inside the VM (seed.py dependency)
ssh ${SSH_OPTS} ubuntu@${VM_IP} "pip3 install --quiet requests"

# Run seed.py three times so we have ~50 sheets and ~750 responses
# (enough volume for the load tests to be meaningful)
for i in 1 2 3; do
    echo "[seed]   Pass ${i}/3 …"
    ssh ${SSH_OPTS} ubuntu@${VM_IP} \
        "cd /opt/correctexam && python3 workload/seed.py --files 357" 2>&1 | grep -E "(ERROR|Import|Done|course)" || true
done

echo "[seed] Done.  DB is populated with 3 courses/exams, ~51 sheets, ~765 responses."
