# Hetzner development host

The selected initial host is one Ubuntu 24.04 VM in Nuremberg (`nbg1`), running Caddy, the production web bundle, API, PostgreSQL 16/PostGIS and one ordinary indexing worker. This Terraform root is independent of the existing AWS root and state. Provider authentication comes only from the local `HCLOUD_TOKEN` environment variable; never put the token in Terraform variables, Git, release archives or chat.

Required before creating resources: the owner's Hetzner project/token, selected available server type and quoted project price, SSH public key plus trusted administrator CIDR, DNS hostname and registered Google **web** client ID for its exact `https://HOSTNAME` origin. The existing local synthetic login must not be exposed remotely. There is no automatic server-size fallback when a cheap plan is unavailable.

The planned resources are one VM with server backups, one retained IPv4, one firewall, one SSH public key and one protected 10 GB journal volume. Only 80/443 are public; SSH is limited to configured CIDRs. PostgreSQL and Uvicorn use local connections. The volume retains the latest deletion journal independently of VM/database snapshots; it is not included in Hetzner server backups. IPv4 and volume remain billed until explicitly removed, and Terraform/cloud deletion protection deliberately prevents accidental removal. Quote VM + 20% server backups + IPv4 + volume in the actual project currency/VAT before applying. Check availability in the project; do not infer it from local validation.

## Provision

Install Terraform 1.5+ or use the existing Nix shell. Set `HCLOUD_TOKEN` locally through a secure environment/configuration method. Copy `terraform.tfvars.example` to ignored `terraform.tfvars`, provide the selected server type, public key and trusted CIDR. Do not reuse an existing unrelated project's state or import an existing host without an explicit migration plan.

```sh
rtk terraform -chdir=infra/hetzner init
rtk terraform -chdir=infra/hetzner plan -out=initial.tfplan
rtk terraform -chdir=infra/hetzner apply initial.tfplan
rtk terraform -chdir=infra/hetzner output
```

Review the saved plan and live price first. Point the selected hostname's A record at `ipv4_address`; add AAAA only for the correct `ipv6_address`. SSH ingress needs the operator's current trusted CIDR. The installer requires the independently attached volume to be mounted at `deletion_journal_mount_path`; it never formats a volume or creates a replacement journal on an existing installation.

## Fresh installation

Use a clean committed checkout with Python application source and locked web dependencies. Build with the registered public Google client ID; the build forces synthetic login off and same-origin API requests. The archive includes tracked backend/deployment files and the generated browser bundle only, excluding local originals, database, dotenv, Terraform state and PoC files.

```sh
rtk npm --prefix apps/web ci
rtk scripts/deploy/build-release.sh GOOGLE_WEB_CLIENT_ID /tmp/city-runner-release.tar.gz
rtk scp /tmp/city-runner-release.tar.gz root@SERVER_IP:/root/
rtk ssh root@SERVER_IP 'mkdir /root/city-runner-release && tar -xzf /root/city-runner-release.tar.gz -C /root/city-runner-release'
rtk ssh root@SERVER_IP 'bash /root/city-runner-release/scripts/deploy/install-hetzner.sh HOSTNAME GOOGLE_WEB_CLIENT_ID /mnt/HC_Volume_VOLUME_ID /root/city-runner-release'
```

Substitute the selected values, including the Terraform volume ID. SSH uses normal host-key verification. The script refuses existing or partial application paths/users/database/role and an unmounted journal volume; do not delete those to make a rerun pass. It creates a non-superuser local database role, private runtime directories, explicitly initializes a new empty journal once, applies migrations and installs systemd API/worker units. The units verify the mount and replay current deletion intents before starting. No provider tokens are installed on the VM.

Both processes share `/etc/city-runner/backend.env`. DB access uses the `cityrunner` Unix-socket/peer-authenticated role; originals and map snapshots remain private VM files. The current journal is `${JOURNAL_MOUNT}/privacy/deletions.jsonl` (0700 parent/0600 file). Application code and browser assets are readable, immutable deployment files; Caddy serves only `apps/web/dist`, never the backend or runtime storage.

The initial host enables the existing bounded **development** Overpass city adapter, disables routing and uses the registered Google client ID. This moves the verified development flow to remote compute; it is not public-production geography/provider acceptance. Public release still requires an approved hosted/owned geography service, country/large-city acceptance and real login verification. Do not replace authentication with a remote synthetic session endpoint.

## Verify before accepting the deployment

Check API/worker/Caddy service status, PostgreSQL readiness and migration head. Through actual HTTPS, verify HTML/assets/client-side fallback return 200, private endpoints return 401 without a bearer, and `/api/dev/session` returns 404 rather than a synthetic login. Complete real Google sign-in with the configured origin/client ID. Use temporary synthetic GPX/FIT only to exercise accepted jobs, automatic city preparation/reuse, duplicate/stop/retry, private account isolation and terminal deletion. Restart the worker and then the host, verify original bytes/timestamps and matching survive, and remove the synthetic account through the API. Local syntax/schema/Caddy checks do not establish any of these hosted results.

## Consistent backup, restore and rollback

Hetzner VM image backups provide a baseline recovery image, not a verified application-consistent database/source backup. Stop API and worker for a consistent checkpoint; keep Caddy from exposing APIs during restore. As root on the host, create a private directory with `umask 077`, use `runuser -u postgres -- pg_dump -Fc city_runner` for the DB, and archive `/var/lib/city-runner/sources` plus `/var/lib/city-runner/map-imports`. Preserve `/etc/city-runner/backend.env`, `/opt/city-runner/RELEASE_COMMIT` and the exact deployed artifact separately with private permissions. Copy the backup off the VM over SSH to operator-selected private storage; local disk alone is not a disaster-recovery backup. Resume the services only after the complete checkpoint succeeds. Schedule/retention and a hosted synthetic restore drill remain required before production acceptance.

The **current journal volume is never rolled back with those backups**. Keep it attached/preserved when rebuilding/restoring the VM and verify its identity and mount before exposing either process. Back up this journal independently to operator-selected private storage as additional protection; restoring an old periodic copy cannot prove that later deletions remain terminal. If the current journal is lost or incomplete, keep the application offline until a complete latest journal is recovered; never initialize an empty replacement.

For a DB/source restore, stop API/worker, restore into an empty isolated database, restore matching sources/map snapshots, point the shared environment at that restored DB and the current mounted journal, then run the existing `python -m backend.app.account_data --restore` and `--status` as `cityrunner`. Preserve/reconcile file ownership to that user's UID. Verify old deleted accounts/files are absent and an unrelated account survives, using the [account recovery contract](../../docs/account-data.md). Start services only after replay/verification; their startup checks are an additional guard. Rehearse this with synthetic data on a separate target before claiming hosted recovery.

For a failed fresh install, leave partial state intact and inspect the failure. For an update rollback, stop services and restore the exact previous code/configuration only if it supports the current schema; otherwise restore the coordinated database/source checkpoint while retaining the latest journal. Do not downgrade migrations or automatically restore a VM image over a live account database. A rollback is an explicit operator action, not a Terraform destroy/recreate.

Sources: [Hetzner provider](https://registry.terraform.io/providers/hetznercloud/hcloud/latest/docs), [volume persistence and backup scope](https://docs.hetzner.com/cloud/volumes/overview/), [billing](https://docs.hetzner.com/cloud/billing/faq/), [Caddy SPA/API routing](https://caddyserver.com/docs/caddyfile/patterns#single-page-apps-spas).
