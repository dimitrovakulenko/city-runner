#!/usr/bin/env bash
# Fresh Ubuntu host only; updates and restores deliberately use a separate runbook.
set -euo pipefail
fail() { echo "$*" >&2; exit 2; }
[[ $# == 3 || $# == 4 ]] || fail 'Usage: install-hetzner.sh DOMAIN GOOGLE_WEB_CLIENT_ID JOURNAL_MOUNT [RELEASE_DIRECTORY]'
domain=$1
google_id=$2
privacy_mount=$3
release_directory=$(realpath "${4:-.}")
[[ $EUID == 0 ]] || fail 'Run as root on the new Hetzner host.'
# shellcheck disable=SC1091
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || fail 'Ubuntu 24.04 is required.'
[[ ${#domain} -le 253 && $domain =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] || fail 'Supply a DNS hostname, without URL or port.'
[[ $google_id =~ ^[A-Za-z0-9-]+\.apps\.googleusercontent\.com$ ]] || fail 'Supply the registered Google web client ID.'
[[ $privacy_mount =~ ^/mnt/HC_Volume_[1-9][0-9]*$ && ! -L $privacy_mount ]] || fail 'Supply the Terraform journal mount path.'
mountpoint -q -- "$privacy_mount" || fail 'The independent journal volume must be mounted.'
[[ -f "$release_directory/backend/requirements.txt" && -f "$release_directory/apps/web/dist/index.html" && -f "$release_directory/RELEASE_COMMIT" ]] || fail 'Release archive is incomplete.'
grep -qE '^[0-9a-f]{40}$' "$release_directory/RELEASE_COMMIT" || fail 'Release commit is invalid.'
grep -RFq -- "$google_id" "$release_directory/apps/web/dist/assets" || fail 'Build the browser with the same Google client ID.'
for path in /opt/city-runner /var/lib/city-runner /etc/city-runner "$privacy_mount/privacy"; do
  [[ ! -e $path && ! -L $path ]] || fail "Existing/partial installation at $path; refusing to initialize over it."
done
if id cityrunner >/dev/null 2>&1; then fail 'The cityrunner user already exists; inspect the partial installation.'; fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv postgresql-16 postgresql-16-postgis-3 caddy
systemctl enable --now postgresql
if [[ $(runuser -u postgres -- psql -Atc "SELECT count(*) FROM pg_roles WHERE rolname='cityrunner'") != 0 ||
      $(runuser -u postgres -- psql -Atc "SELECT count(*) FROM pg_database WHERE datname='city_runner'") != 0 ]]; then
  fail 'Existing City Runner database/role; refusing fresh initialization.'
fi
useradd --system --user-group --home-dir /var/lib/city-runner --shell /usr/sbin/nologin cityrunner
umask 022
install -d /opt/city-runner /opt/city-runner/apps/web
cp -a "$release_directory/backend" /opt/city-runner/
cp -a "$release_directory/apps/web/dist" /opt/city-runner/apps/web/
cp "$release_directory/RELEASE_COMMIT" /opt/city-runner/
chown -R root:root /opt/city-runner
chmod -R a+rX /opt/city-runner
python3 -m venv /opt/city-runner/.venv
/opt/city-runner/.venv/bin/pip install -r /opt/city-runner/backend/requirements.txt
umask 077
install -d -m 0700 -o cityrunner -g cityrunner /var/lib/city-runner /var/lib/city-runner/sources /var/lib/city-runner/map-imports "$privacy_mount/privacy"
install -d -m 0750 -o root -g cityrunner /etc/city-runner
cat > /etc/city-runner/backend.env <<EOF
DATABASE_URL="postgresql+psycopg:///city_runner?host=/var/run/postgresql"
GOOGLE_CLIENT_IDS="$google_id"
UPLOAD_STORAGE_DIR="/var/lib/city-runner/sources"
MAP_IMPORT_STORAGE_ROOT="/var/lib/city-runner/map-imports"
ACCOUNT_DELETION_LEDGER="$privacy_mount/privacy/deletions.jsonl"
PYTHONDONTWRITEBYTECODE=1
CITY_GEOGRAPHY_PROVIDER=overpass
ROUTING_PROVIDER=disabled
EOF
chown root:cityrunner /etc/city-runner/backend.env
chmod 0640 /etc/city-runner/backend.env
runuser -u postgres -- createuser --no-superuser --no-createdb --no-createrole cityrunner
runuser -u postgres -- createdb --owner=cityrunner city_runner
runuser -u postgres -- psql --dbname=city_runner --command='CREATE EXTENSION postgis'
cd /opt/city-runner
runuser -u cityrunner -- env PYTHONDONTWRITEBYTECODE=1 DATABASE_URL='postgresql+psycopg:///city_runner?host=/var/run/postgresql' \
  /opt/city-runner/.venv/bin/python -m alembic -c backend/alembic.ini upgrade head
runuser -u cityrunner -- env PYTHONDONTWRITEBYTECODE=1 ACCOUNT_DELETION_LEDGER="$privacy_mount/privacy/deletions.jsonl" \
  /opt/city-runner/.venv/bin/python -m backend.app.account_data --init-ledger
templates="$release_directory/infra/hetzner/templates"
for unit in api worker; do
  sed "s|\${journal_mount}|$privacy_mount|g" "$templates/$unit.service.tftpl" > "/etc/systemd/system/city-runner-$unit.service"
  chmod 0644 "/etc/systemd/system/city-runner-$unit.service"
done
sed "s|\${domain}|$domain|g" "$templates/Caddyfile.tftpl" > /etc/caddy/Caddyfile
chmod 0644 /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable --now city-runner-api city-runner-worker
systemctl reload caddy
for unit in city-runner-api city-runner-worker caddy; do
  systemctl is-active --quiet "$unit" || fail "$unit is not active; inspect its journal."
done
echo "Installed. Point DNS at this host, then verify https://$domain and real Google sign-in."
