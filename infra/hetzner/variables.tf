variable "location" {
  description = "Hetzner Cloud location for the VM and retained IPv4 address."
  type        = string
  default     = "nbg1"
}

variable "server_type" {
  description = "Explicit Hetzner server type. Choose a size for the expected workload and budget."
  type        = string

  validation {
    condition     = length(trimspace(var.server_type)) > 0
    error_message = "Set server_type explicitly; there is no default server size."
  }
}

variable "ssh_public_key" {
  description = "OpenSSH public key installed on the Ubuntu VM. Never provide a private key."
  type        = string

  validation {
    condition = can(regex(
      "^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,2}( .*)?$",
      trimspace(var.ssh_public_key)
    ))
    error_message = "ssh_public_key must be an OpenSSH Ed25519, RSA, or P-256 public key."
  }
}

variable "ssh_allowed_cidrs" {
  description = "Trusted IPv4/IPv6 CIDRs allowed to connect on SSH port 22. Empty disables public SSH."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.ssh_allowed_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every SSH source must be a valid IPv4 or IPv6 CIDR."
  }
}
