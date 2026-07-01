variable "vm_name" {
  description = "Name of the VM — also used as the experiment label in result filenames."
  type        = string
  default     = "correctexam-exp"
}

variable "vm_vcpus" {
  description = "Number of vCPUs to allocate to the experiment VM."
  type        = number
  default     = 2
}

variable "vm_ram_mb" {
  description = "RAM to allocate to the experiment VM, in megabytes."
  type        = number
  default     = 4096
}

variable "vm_disk_gb" {
  description = "Root disk size for the experiment VM, in gigabytes."
  type        = number
  default     = 20
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key file to inject into the VM."
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

variable "libvirt_uri" {
  description = "libvirt connection URI.  On the Grid5000 node: qemu:///system"
  type        = string
  default     = "qemu:///system"
}

variable "storage_pool" {
  description = "libvirt storage pool to use for VM disks."
  type        = string
  default     = "default"
}

# Ubuntu 22.04 LTS cloud image (downloaded once, reused as CoW base for all VMs)
variable "base_image_url" {
  description = "URL of the Ubuntu 22.04 cloud image."
  type        = string
  default     = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
}
