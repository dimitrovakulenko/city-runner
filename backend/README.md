# Backend

Install `requirements.txt`, set `DATABASE_URL` to a PostgreSQL database with PostGIS installed, then run `alembic -c backend/alembic.ini upgrade head` from the repository root. Inspect generated migration SQL from the same directory with `alembic -c backend/alembic.ini upgrade head --sql`. Set `GOOGLE_CLIENT_IDS` and/or `APPLE_CLIENT_IDS` to comma-separated registered client IDs to enable those sign-in providers. Start the API with `uvicorn backend.app.main:app`.

Run the synthetic API tests with `python -m unittest discover -s backend/tests`. They use SQLite and cover the activity explorer contract, account isolation, provider token verification, challenge replay, and session revocation. Run migration and concurrency checks against a disposable PostgreSQL/PostGIS database with `scripts/dev/postgis-test.sh`. Sessions last 30 days by default; `SESSION_LIFETIME_DAYS` accepts values from 1 through 90.
