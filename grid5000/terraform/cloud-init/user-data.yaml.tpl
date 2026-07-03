#cloud-config
# cloud-init user-data for experiment VMs.
# Installs Docker and configures SSH access.
# Python + seed script runs later via deploy_app.sh (no need to install here).

hostname: ${vm_name}
fqdn: ${vm_name}.local

users:
  - name: ubuntu
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - ${ssh_public_key}

# Speed up package install: only update indexes, skip full upgrade
package_update: true
package_upgrade: false

packages:
  - docker.io
  - python3
  - python3-pip
  - curl
  - jq

runcmd:
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
  - |
    curl -fsSL \
      https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
      -o /usr/local/bin/docker-compose
  - chmod +x /usr/local/bin/docker-compose
  - pip3 install --quiet requests
  # 4 GB swap on the VM disk — OS-level resource, prevents OOM kills during startup peak.
  # Documented in experiments as a baseline VM configuration for all resource configs.
  - fallocate -l 4G /swapfile
  - chmod 600 /swapfile
  - mkswap /swapfile
  - swapon /swapfile
  - echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Make cloud-init status visible in the journal
final_message: "CorrectExam experiment VM ${vm_name} is ready (uptime: $UPTIME seconds)"
