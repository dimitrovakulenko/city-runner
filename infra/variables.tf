variable "region" {
  description = "AWS region for Lightsail and S3."
  type        = string
}

variable "domain" {
  description = "Public DNS name served by Caddy."
  type        = string
}

variable "instance_bundle_id" {
  description = "Lightsail bundle identifier that controls VM size."
  type        = string
  default     = "small_3_0"
}

variable "bucket_name" {
  description = "Globally unique name for private application storage."
  type        = string
}

variable "ssh_allowed_cidr" {
  description = "Optional CIDR allowed to SSH to the VM. Empty disables SSH ingress."
  type        = string
  default     = ""
}
