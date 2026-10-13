#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ! $1 =~ ^[A-Za-z0-9-]+\.apps\.googleusercontent\.com$ ]]; then
  echo 'Usage: build-release.sh GOOGLE_WEB_CLIENT_ID NEW_ARCHIVE.tar.gz' >&2
  exit 2
fi
root=$(cd "$(dirname "$0")/../.." && pwd)
archive=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
[[ ! -e "$archive" ]] || { echo 'Archive already exists.' >&2; exit 2; }
cd "$root"
git diff --quiet
git diff --cached --quiet
umask 077
release_stage=$(mktemp -d "${TMPDIR:-/tmp}/city-runner-release.XXXXXX")
trap 'rm -rf -- "$release_stage"' EXIT
(
  cd apps/web
  env VITE_GOOGLE_CLIENT_ID="$1" VITE_DEV_TEST_LOGIN=0 VITE_API_BASE_URL='' npm run build
)
# Archive tracked application code only: no local sources, database, dotenv or PoC.
git archive --format=tar HEAD backend infra/hetzner/templates scripts/deploy > "$release_stage/release.tar"
git rev-parse HEAD > "$release_stage/RELEASE_COMMIT"
tar -rf "$release_stage/release.tar" -C "$root" apps/web/dist
tar -rf "$release_stage/release.tar" -C "$release_stage" RELEASE_COMMIT
gzip -c "$release_stage/release.tar" > "$archive"
echo "Built $archive"
