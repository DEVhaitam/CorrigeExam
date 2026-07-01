#!/usr/bin/env bash
# deploy_app.sh — Deploy the CorrectExam app stack on an experiment VM.
#
# Must run ON the bare-metal node (not from your laptop).
# The VM must already exist (Terraform applied) and be reachable via SSH.
#
# Usage:
#   bash grid5000/scripts/deploy_app.sh <vm_ip> <ssh_key_path>
#
# Example:
#   bash grid5000/scripts/deploy_app.sh 192.168.122.10 ~/.ssh/id_rsa
set -euo pipefail

VM_IP="${1:?Usage: deploy_app.sh <vm_ip> <ssh_key_path>}"
SSH_KEY="${2:-${HOME}/.ssh/id_rsa}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30 -i ${SSH_KEY}"
SCP_OPTS="-o StrictHostKeyChecking=no -i ${SSH_KEY}"

_ssh()  { ssh  ${SSH_OPTS} ubuntu@${VM_IP} "$@"; }
_scp()  { scp  ${SCP_OPTS} "$@"; }

echo "[deploy] Target VM: ${VM_IP}"

echo "[1/5] Waiting for SSH …"
until _ssh "echo ok" 2>/dev/null; do
    sleep 5
done

echo "[2/5] Copying app files to VM …"
_ssh "mkdir -p /opt/correctexam/{workload,testData}"

# Copy the docker-compose (VM variant — no Prometheus/Grafana, full bind-mounts for node-exporter)
_scp "${REPO_ROOT}/grid5000/vm-docker-compose.yml"  ubuntu@${VM_IP}:/opt/correctexam/docker-compose.yml

# Copy the seed script and test data
_scp "${REPO_ROOT}/workload/seed.py"                ubuntu@${VM_IP}:/opt/correctexam/workload/
_scp "${REPO_ROOT}/testData/357.json"               ubuntu@${VM_IP}:/opt/correctexam/testData/

echo "[3/5] Pulling Docker images on VM (may take a few minutes the first time) …"
_ssh "cd /opt/correctexam && sudo docker compose pull --quiet 2>&1 | tail -5"

echo "[4/5] Starting app stack …"
_ssh "cd /opt/correctexam && sudo docker compose up -d"

echo "[5/5] Waiting for backend to be healthy …"
MAX_WAIT=180
ELAPSED=0
until _ssh "curl -sf http://localhost:8082/management/health/live > /dev/null 2>&1"; do
    if [ "${ELAPSED}" -ge "${MAX_WAIT}" ]; then
        echo "[deploy] ERROR: backend did not become healthy after ${MAX_WAIT}s"
        _ssh "sudo docker compose logs back --tail 40" || true
        exit 1
    fi
    sleep 10
    ELAPSED=$((ELAPSED + 10))
    echo "[deploy]   waited ${ELAPSED}s …"
done

echo ""
echo "[deploy] App is up at http://${VM_IP}:8082"
echo "[deploy] Now seed the DB:  bash grid5000/scripts/seed_vm.sh ${VM_IP} ${SSH_KEY}"
