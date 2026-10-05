resource "aws_lightsail_instance" "app" {
  name              = "city-runner"
  availability_zone = "${var.region}a"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = var.instance_bundle_id
}

resource "aws_lightsail_static_ip" "app" {
  name = "city-runner"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.name
  instance_name  = aws_lightsail_instance.app.name
}

resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
  }

  dynamic "port_info" {
    for_each = var.ssh_allowed_cidr == "" ? [] : [var.ssh_allowed_cidr]
    content {
      protocol   = "tcp"
      from_port  = 22
      to_port    = 22
      cidrs      = [port_info.value]
      ipv6_cidrs = []
    }
  }
}

resource "aws_s3_bucket" "private_storage" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "private_storage" {
  bucket                  = aws_s3_bucket.private_storage.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "private_storage" {
  bucket = aws_s3_bucket.private_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
