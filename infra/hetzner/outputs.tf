output "server_id" {
  description = "Hetzner Cloud server ID."
  value       = hcloud_server.app.id
}

output "server_type" {
  description = "Explicit server size selected for this deployment."
  value       = hcloud_server.app.server_type
}

output "location" {
  description = "Hetzner Cloud location hosting the VM and retained IPv4 address."
  value       = hcloud_server.app.location
}

output "ipv4_address" {
  description = "Retained primary IPv4 address for DNS and HTTPS ingress."
  value       = hcloud_primary_ip.app_ipv4.ip_address
}

output "ipv6_address" {
  description = "Server IPv6 address for DNS and HTTPS ingress."
  value       = hcloud_server.app.ipv6_address
}

output "deletion_journal_mount_path" {
  description = "Automounted protected ext4 volume path for the account-deletion journal."
  value       = "/mnt/HC_Volume_${hcloud_volume.deletion_journal.id}"
}

output "deletion_journal_volume_size_gb" {
  description = "Protected independent volume capacity; billed separately from the VM."
  value       = hcloud_volume.deletion_journal.size
}
