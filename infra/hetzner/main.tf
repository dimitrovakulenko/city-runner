resource "hcloud_ssh_key" "app" {
  name       = "city-runner"
  public_key = var.ssh_public_key
}

resource "hcloud_firewall" "app" {
  name = "city-runner-web"

  rule {
    description = "HTTP"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "HTTPS"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  dynamic "rule" {
    for_each = length(var.ssh_allowed_cidrs) == 0 ? [] : [var.ssh_allowed_cidrs]
    content {
      description = "SSH from configured trusted CIDRs"
      direction   = "in"
      protocol    = "tcp"
      port        = "22"
      source_ips  = rule.value
    }
  }
}

resource "hcloud_primary_ip" "app_ipv4" {
  name              = "city-runner-ipv4"
  location          = var.location
  type              = "ipv4"
  auto_delete       = false
  delete_protection = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "hcloud_server" "app" {
  name         = "city-runner"
  image        = "ubuntu-24.04"
  server_type  = var.server_type
  location     = var.location
  backups      = true
  ssh_keys     = [hcloud_ssh_key.app.id]
  firewall_ids = [hcloud_firewall.app.id]

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.app_ipv4.id
    ipv6_enabled = true
  }
}

resource "hcloud_volume" "deletion_journal" {
  name              = "city-runner-deletion-journal"
  size              = 10
  server_id         = hcloud_server.app.id
  automount         = true
  format            = "ext4"
  delete_protection = true

  lifecycle {
    prevent_destroy = true
  }
}
