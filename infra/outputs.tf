output "static_ip_address" {
  description = "Lightsail address to point the domain at."
  value       = aws_lightsail_static_ip.app.ip_address
}

output "private_bucket_name" {
  description = "Private S3 bucket for application storage."
  value       = aws_s3_bucket.private_storage.id
}

output "caddy_configuration" {
  description = "Caddy configuration rendered for the configured domain."
  value       = templatefile("${path.module}/templates/Caddyfile.tftpl", { domain = var.domain })
}
