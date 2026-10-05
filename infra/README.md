# Production infrastructure

Terraform defines one Ubuntu Lightsail VM with a static IP and a private, encrypted S3 bucket. Set `region`, `domain`, and `bucket_name` in a local `*.tfvars` file; `instance_bundle_id` selects the VM size. SSH ingress is closed unless `ssh_allowed_cidr` is set to a trusted CIDR.

The files in `templates/` are deployment templates: install backend dependencies from `backend/requirements.txt`, install PostgreSQL, and create the `cityrunner` account before installing the systemd units. Configure `/etc/city-runner/backend.env` outside Terraform. Render `Caddyfile.tftpl` with the chosen domain and install Caddy separately.

Terraform does not install software or attach AWS credentials to the VM. Use an AWS identity with the required Lightsail and S3 permissions when applying; no credentials belong in this directory.
