terraform {
  required_version = ">= 1.5"
  required_providers {
    libvirt = {
      source  = "dmacvicar/libvirt"
      version = "0.7.6"
    }
  }
}

provider "libvirt" {
  uri = var.libvirt_uri
}

# ── Storage: base image (downloaded once, shared across all experiments) ──────

resource "libvirt_volume" "base_image" {
  name   = "ubuntu-22.04-base.qcow2"
  pool   = var.storage_pool
  source = var.base_image_url
  format = "qcow2"

  # The base image is intentionally NOT destroyed when terraform destroy runs
  # so subsequent experiments can reuse it without re-downloading (saves ~5 min).
  # Delete it manually when done with ALL experiments:
  #   virsh vol-delete ubuntu-22.04-base.qcow2 --pool default
  lifecycle {
    prevent_destroy = false
  }
}

# CoW overlay — each experiment VM gets its own overlay on top of the base.
# The VM can write freely; the base image is never modified.
resource "libvirt_volume" "vm_disk" {
  name           = "${var.vm_name}-disk.qcow2"
  pool           = var.storage_pool
  base_volume_id = libvirt_volume.base_image.id
  format         = "qcow2"
  size           = var.vm_disk_gb * 1024 * 1024 * 1024
}

# ── Cloud-init ────────────────────────────────────────────────────────────────

resource "libvirt_cloudinit_disk" "vm_init" {
  name      = "${var.vm_name}-cloudinit.iso"
  pool      = var.storage_pool
  user_data = templatefile(
    "${path.module}/cloud-init/user-data.yaml.tpl",
    {
      vm_name        = var.vm_name
      ssh_public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
    }
  )
}

# ── VM definition ─────────────────────────────────────────────────────────────

resource "libvirt_domain" "vm" {
  name   = var.vm_name
  memory = var.vm_ram_mb
  vcpu   = var.vm_vcpus

  cloudinit = libvirt_cloudinit_disk.vm_init.id

  # host-passthrough exposes the physical CPU features to the guest,
  # which is important for JVM JIT performance (AVX, AES-NI, etc.).
  cpu {
    mode = "host-passthrough"
  }

  disk {
    volume_id = libvirt_volume.vm_disk.id
  }

  # Use the libvirt default NAT network.
  # VMs get IPs in the 192.168.122.0/24 range via DHCP.
  # The bare-metal node can reach VMs directly at these IPs.
  network_interface {
    network_name   = "default"
    wait_for_lease = true   # block until DHCP gives the VM an IP
  }

  # Serial console — useful for debugging if SSH doesn't work
  console {
    type        = "pty"
    target_port = "0"
    target_type = "serial"
  }

  # Give cloud-init enough time to finish before Terraform returns
  provisioner "remote-exec" {
    inline = [
      "cloud-init status --wait --long",
      "echo 'cloud-init finished'",
    ]
    connection {
      type        = "ssh"
      user        = "ubuntu"
      host        = self.network_interface[0].addresses[0]
      private_key = file(replace(pathexpand(var.ssh_public_key_path), ".pub", ""))
      timeout     = "10m"
    }
  }
}
