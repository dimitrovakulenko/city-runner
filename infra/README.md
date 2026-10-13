# Production infrastructure

Terraform defines one Ubuntu Lightsail VM with a static IP and a private, encrypted S3 bucket. Set `region`, `domain`, and `bucket_name` in a local `*.tfvars` file; `instance_bundle_id` selects the VM size. SSH ingress is closed unless `ssh_allowed_cidr` is set to a trusted CIDR.

The files in `templates/` are deployment templates: install backend dependencies from `backend/requirements.txt`, install PostgreSQL, and create the `cityrunner` account before installing the systemd units. Configure `/etc/city-runner/backend.env` outside Terraform. Render `Caddyfile.tftpl` with the chosen domain and install Caddy separately.

Terraform does not install software or attach AWS credentials to the VM. Use an AWS identity with the required Lightsail and S3 permissions when applying; no credentials belong in this directory.

The API and `worker.service.tftpl` use the same `/etc/city-runner/backend.env` and PostgreSQL database. Install the worker unit as `city-runner-worker.service` on the selected host after applying migrations. Its ordinary job loop handles accepted uploads, matching, shared city preparation and immutable map imports. Multiple ordinary workers share database leases/admission; geography preparation must not run against a different private source store or deletion journal.

For the development city adapter, set `CITY_GEOGRAPHY_PROVIDER=overpass` on both API and worker, and configure durable `MAP_IMPORT_STORAGE_ROOT`, `UPLOAD_STORAGE_DIR` and `ACCOUNT_DELETION_LEDGER` under private application storage. `CITY_OVERPASS_URL` can select a trusted HTTPS Overpass-compatible data service. The default public service is for bounded development checks; a public deployment needs an approved hosted/owned data service. Country municipal policies can be added through `CITY_ADMIN_LEVELS`, a JSON object of ISO country codes to explicit admin-level arrays. See [city provisioning](../docs/city-provisioning.md) for privacy, bounds and release gaps.

These templates do not provision or start a worker by themselves. Deployment requires a selected AWS account/server and its application configuration; do not infer that a local test or template establishes hosted execution.
