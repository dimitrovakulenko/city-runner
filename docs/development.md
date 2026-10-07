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

Set `DATABASE_URL=postgresql+psycopg:///activities` for a local Unix-socket connection (or an explicit authenticated URL for TCP), apply migrations with `rtk .venv/bin/python -m alembic -c backend/alembic.ini upgrade head`, then start the API with `rtk .venv/bin/uvicorn backend.app.main:app --reload --port 8001`. SQLite unit tests remain available with `rtk .venv/bin/python -m unittest discover -s backend/tests`.

Set registered `GOOGLE_CLIENT_IDS` and/or `APPLE_CLIENT_IDS` to enable provider challenges and identity exchange. Without them login fails closed. The mobile Google adapter and secure-session store are implemented; configure public mobile client IDs as described in [mobile login](mobile-login.md). Real native/provider acceptance remains pending; backend verification uses synthetic signed identities. Run the worker separately with `rtk .venv/bin/python -m backend.app.worker`; `--once` handles at most one job. Authenticated [GPX uploads](gpx-import.md) now enqueue real parsing jobs. API and worker must share `UPLOAD_STORAGE_DIR` (default `backend/.uploads`, ignored by Git). [OSM import](osm-import.md) runs as a separate command using public extracts; [coverage matching](coverage.md) and [map/progress](map-api.md) run in the production backend. The first complete dataset can activate; replacements remain staged. [City/street APIs](explorer-api.md) expose version-qualified progress and contributions.

The PostGIS integration runner creates a UUID-named `city_runner_test_*` database, runs migration up/down/up and synthetic owner/search/track API checks, then drops that database even when tests fail. It requires a PostgreSQL administrator URL that can create/drop databases and install the PostGIS extension. Run `rtk proxy env PATH="$PWD/.venv/bin:$PATH" scripts/dev/postgis-test.sh` from the repository root. The default admin URL uses the local Unix socket and current OS username. To select another local cluster, set `POSTGIS_ADMIN_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/postgres`. The test URL is derived from the same admin URL, and the harness overrides any inherited `DATABASE_URL` before running migrations.

## Account data and history scale

Account data operations use the [export/deletion contract and recovery commands](account-data.md). Configure and explicitly initialize the independent deletion journal for a fresh installation; share its path between API and worker. Missing configuration disables full account deletion, while a configured missing/corrupt journal fails closed. Never initialize a replacement journal during restore. The [synthetic history benchmark](history-benchmark.md) records the local 5-million-sample workload and measured map fix.

## Mobile checks and development app

From `apps/mobile`, run `rtk npm ci` followed by `rtk npm run typecheck` and `rtk npm test`. `npm ci` uses the checked-in `package-lock.json`.

The production API uses port 8001 so the PoC can remain on 8000. Run the worker alongside it, including for queued private-file cleanup after [activity deletion](corrections.md). Follow [mobile login](mobile-login.md), [Explore/import](mobile-explore.md) and [city/street browsing](mobile-cities.md) to connect a development build; Android emulator uses `http://10.0.2.2:8001`. OAuth configuration and native SDK/runtime installation remain acceptance gates. The PoC browser is still a comparison reference.

## Browser UI

From `apps/web`, run `rtk npm ci` and `rtk npm run dev`; open `http://127.0.0.1:5173`. The same-origin `/api` proxy targets the production API on port 8001. Start the API and worker as above. The [browser client README](../apps/web/README.md) covers web Google OAuth configuration, shared client contracts, resumable file imports and disposable synthetic Chrome checks. Run `rtk npm test` and `rtk npm run build` for adapter tests, typechecking and production assets.
