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
  - docker-compose
  - python3
  - python3-pip
  - curl
  - jq

runcmd:
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
  - pip3 install --quiet requests

# Make cloud-init status visible in the journal
final_message: "CorrectExam experiment VM ${vm_name} is ready (uptime: $UPTIME seconds)"
