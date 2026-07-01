output "vm_ip" {
  description = "IP address of the experiment VM (libvirt NAT, reachable from the bare-metal node)."
  value       = libvirt_domain.vm.network_interface[0].addresses[0]
}

output "vm_name" {
  description = "Name of the experiment VM."
  value       = libvirt_domain.vm.name
}

output "vm_ram_mb" {
  description = "RAM allocated to the VM in MB."
  value       = libvirt_domain.vm.memory
}

output "vm_vcpus" {
  description = "vCPU count allocated to the VM."
  value       = libvirt_domain.vm.vcpu
}
