# Development

## Backend and PostGIS

The backend runs as ordinary local processes. Python 3.11+ and PostgreSQL 16 with PostGIS are required. Install prerequisites with Homebrew (`rtk brew install postgresql@16 postgis`) or on Ubuntu (`rtk sudo apt install postgresql postgresql-postgis`). Start the local PostgreSQL service (`rtk brew services start postgresql@16` or `rtk sudo service postgresql start`).

From the repository root, set up Python and create the local development database:

```sh
rtk python3 -m venv .venv
rtk .venv/bin/pip install -r backend/requirements.txt -r requirements-poc.txt
rtk createdb activities
rtk psql activities -c 'CREATE EXTENSION IF NOT EXISTS postgis'
```

Set `DATABASE_URL=postgresql+psycopg:///activities` for a local Unix-socket connection (or an explicit authenticated URL for TCP), apply migrations with `rtk .venv/bin/python -m alembic -c backend/alembic.ini upgrade head`, then start the API with `rtk .venv/bin/uvicorn backend.app.main:app --reload`. SQLite unit tests remain available with `rtk .venv/bin/python -m unittest discover -s backend/tests`.

The PostGIS integration runner creates a UUID-named `city_runner_test_*` database, runs migration up/down/up and synthetic owner/search/track API checks, then drops that database even when tests fail. It requires a PostgreSQL administrator URL that can create/drop databases and install the PostGIS extension. Run `rtk proxy env PATH="$PWD/.venv/bin:$PATH" scripts/dev/postgis-test.sh` from the repository root. The default admin URL uses the local Unix socket and current OS username. To select another local cluster, set `POSTGIS_ADMIN_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/postgres`. The test URL is derived from the same admin URL, and the harness overrides any inherited `DATABASE_URL` before running migrations.

## Mobile typecheck

From `apps/mobile`, run `rtk npm ci` followed by `rtk npm run typecheck`. `npm ci` uses the checked-in `package-lock.json`.
