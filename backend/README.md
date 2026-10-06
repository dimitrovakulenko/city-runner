# Backend

Install `requirements.txt`, set `DATABASE_URL` to a PostgreSQL database with PostGIS installed, then run `alembic -c backend/alembic.ini upgrade head` from the repository root. Inspect generated migration SQL from the same directory with `alembic -c backend/alembic.ini upgrade head --sql`. Set `GOOGLE_CLIENT_IDS` and/or `APPLE_CLIENT_IDS` to comma-separated registered client IDs to enable those sign-in providers. Start the API with `uvicorn backend.app.main:app`.

Run the synthetic API tests with `python -m unittest discover -s backend/tests`. They use SQLite and cover the activity explorer contract, account isolation, provider token verification, challenge replay, and session revocation. Run migration and concurrency checks against a disposable PostgreSQL/PostGIS database with `scripts/dev/postgis-test.sh`. Sessions last 30 days by default; `SESSION_LIFETIME_DAYS` accepts values from 1 through 90.

Authenticated `GET /api/map` and `/api/progress` expose bounded viewport layers and versioned lifetime coverage. See [query parameters, readiness and limits](../docs/map-api.md). Native login and the connected mobile import/coverage UI remain pending.

Start the durable worker with `python -m backend.app.worker`, or use `--once` to claim at most one job. It handles `process_upload`, `match_coverage` and synthetic `dev.noop` jobs. Jobs support transactional enqueue, priority, bounded retries, expired-lease recovery and account-scoped cancellation. GPX source/activity writes and job completion are lease/revision-checked in one transaction. See [GPX uploads](../docs/gpx-import.md), [regional OSM import](../docs/osm-import.md) and [coverage matching](../docs/coverage.md); hosted S3 source storage remains pending.
