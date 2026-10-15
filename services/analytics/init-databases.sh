#!/usr/bin/env bash
set -euo pipefail
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set metadata_password="$SUPERSET_METADATA_PASSWORD" <<'SQL'
CREATE USER superset_meta PASSWORD :'metadata_password';
CREATE DATABASE superset_meta OWNER superset_meta;
SQL
