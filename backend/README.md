# Backend

Install `requirements.txt`, set `DATABASE_URL` to a PostgreSQL database with PostGIS installed, then run `alembic -c backend/alembic.ini upgrade head` from the repository root. Start the API with `uvicorn backend.app.main:app`.

Run the synthetic API tests with `python -m unittest discover -s backend/tests`. They use SQLite and cover the activity explorer contract, account isolation, and persistence after restart. The local PostgreSQL services checked for this change do not provide PostGIS, so the Alembic migration and API search have not been verified against PostgreSQL/PostGIS.
