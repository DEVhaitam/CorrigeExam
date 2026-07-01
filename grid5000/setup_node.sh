#!/usr/bin/env bash
# setup_node.sh — Run ONCE on the reserved Grid5000 node.
#
# Installs: Docker, Terraform, libvirt/KVM tools, Python + Locust.
# Starts: monitoring stack (Prometheus + Grafana) as Docker containers.
#
# Usage (from the node):
#   cd /root/CorrigeExam
#   bash grid5000/setup_node.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TERRAFORM_VERSION="1.9.5"
ARCH="linux_amd64"

echo "=== [1/5] Installing system packages ==="
apt-get update -qq
apt-get install -y -q \
    docker.io \
    docker-compose-plugin \
    qemu-kvm \
    libvirt-daemon-system \
    libvirt-clients \
    virtinst \
    genisoimage \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    wget \
    unzip \
    jq

systemctl enable --now docker
systemctl enable --now libvirtd

# Add root to docker and libvirt groups (effective immediately for scripts)
usermod -aG docker root 2>/dev/null || true
usermod -aG libvirt root 2>/dev/null || true

echo "=== [2/5] Installing Terraform ${TERRAFORM_VERSION} ==="
if ! command -v terraform &>/dev/null; then
    TF_URL="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_${ARCH}.zip"
    wget -q "$TF_URL" -O /tmp/terraform.zip
    unzip -o /tmp/terraform.zip -d /usr/local/bin/
    rm /tmp/terraform.zip
    terraform version
else
    echo "Terraform already installed: $(terraform version --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"terraform_version\"])')"
fi

echo "=== [3/5] Installing Python dependencies ==="
pip3 install --quiet locust pyyaml requests

echo "=== [4/5] Initialising Terraform libvirt provider ==="
cd "${REPO_ROOT}/grid5000/terraform"
terraform init -input=false

echo "=== [5/5] Starting monitoring stack on the node ==="
cd "${REPO_ROOT}/grid5000/monitoring"

# Create empty target files so Prometheus starts without errors
for f in vm_node.json vm_cadvisor.json vm_app.json vm_mysql.json; do
    [ -f "targets/${f}" ] || echo '[]' > "targets/${f}"
done

docker compose -f docker-compose.mon.yml up -d

echo ""
echo "========================================================"
echo "Node setup complete."
echo ""
echo "Monitoring:"
echo "  Grafana    -> http://localhost:3000  (admin/admin)"
echo "  Prometheus -> http://localhost:9092"
echo ""
echo "SSH tunnel from your laptop:"
NODE=$(hostname -f)
echo "  ssh -N -L 3000:localhost:3000 -L 9092:localhost:9092 ${NODE}"
echo ""
echo "Run experiments:"
echo "  cd /root/CorrigeExam"
echo "  python3 grid5000/scripts/run_experiment.py --all"
echo "========================================================"
