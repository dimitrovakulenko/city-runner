#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

python - <<'PY'
import os
import subprocess
import sys
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

admin_url = make_url(os.getenv("POSTGIS_ADMIN_DATABASE_URL", "postgresql+psycopg:///postgres"))
database = f"city_runner_test_{uuid.uuid4().hex[:12]}"
maintenance_url = admin_url.set(database="postgres")
engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
created = False
result = 1
try:
    with engine.connect() as db:
        db.exec_driver_sql(f'CREATE DATABASE "{database}"')
    created = True
    test_url = admin_url.set(database=database).render_as_string(hide_password=False)
    env = os.environ.copy()
    env["POSTGIS_TEST_DATABASE_URL"] = test_url
    env["DATABASE_URL"] = test_url
    print(f"Running PostGIS integration tests in disposable database {database}", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "backend/tests/integration", "-v"],
        env=env,
        check=False,
    ).returncode
finally:
    if created:
        with engine.connect() as db:
            db.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid <> pg_backend_pid()"), {"name": database})
            db.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    engine.dispose()
raise SystemExit(result)
PY
