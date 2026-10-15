# Personal analytics operations

The opt-in local services are in `services/analytics/compose.yaml`. They use the existing application database and original-file/deletion-ledger volume, plus an isolated PostgreSQL instance containing the scalar analytics projection and Superset metadata. Superset 6.1.0 and all base images are pinned by digest. Redis, Celery, asynchronous SQL and query/result caches are disabled. Cloud installation remains D34.

Copy `services/analytics/.env.example` outside the repository with mode 0600. Supply distinct random secrets of at least 32 bytes for `SUPERSET_SECRET_KEY` and `ANALYTICS_SECRET`, and strong hexadecimal database passwords. Keep these values stable across restarts and backups: changing `ANALYTICS_SECRET` invalidates derived database credentials. Never place admin database URLs, the shared secret or Superset credentials in a frontend environment variable.

The first integration supports separate origins on the **same hostname and scheme**, using different ports. Production requires HTTPS terminated by an operator-managed reverse proxy; loopback HTTP is supported for local acceptance. Publish only the gateway and existing application API through that proxy. Superset and PostgreSQL remain internal. Strict HttpOnly cookies and exact-origin POST handoff depend on this topology; arbitrary sibling domains are not supported by this version.

Before starting imports/projector/gateway, migrate the application and initialize its existing durable deletion ledger. For a new local installation:

```sh
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml run --rm --build app python -m alembic -c backend/alembic.ini upgrade head
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml run --rm app python -m backend.app.account_data --init-ledger
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml up -d --build --wait
```

Use the same primary database, originals directory and deletion ledger as the ordinary API/import worker. The sample stack's application API binds loopback port 8001; configure the web proxy accordingly. Native provider registration and cloud networking are separate work.

## Refresh and admission

Opening Insights registers an owner for bounded projection. Selecting a region is explicit; without one, only activity statistics are available. The dedicated projector polls every five seconds, defers owners with active upload/matching jobs, and admits one refresh globally using a primary-database advisory lock. A snapshot has a 30-second total budget, 1.5-second statements and the documented activity/relation limits. Source revision/generation validation withholds stale snapshots and in-flight results.

There is one active gateway instance, enforced by a durable 30-second database lease. Local counters permit two SQL requests per owner/four globally, separately bounded metadata and cancellation work, and short bounded preview queues. Replacement processes reconcile interrupted durable admissions before serving. Running a second gateway cannot multiply the SQL budget. A crash may delay startup for the previous lease to expire.

Database work has an immutable eight-second deadline, verified PID/backend-start termination, 10,000 fetched rows, 16 MiB spill, a 1 MiB request and 8 MiB dynamic response limit. SQL authors cannot relax the external timer. Container limits bound oversized client buffering; exceeding a limit fails the request instead of publishing partial totals. Boundary failures persist an owner fence. Logs report only a failure category, never SQL text, results or credentials.

## Recovery and deletion

For a boundary failure, inspect service health and the owner's isolated database sessions using operator credentials. The owner key is the SHA-256 of the account ID, available in the primary `analytics_accounts` registry. Run:

```sh
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml exec -T projector python -m backend.app.analytics_worker --recover-owner OWNER_SHA256
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml restart gateway
```

Recovery verifies that owner database work has stopped, clears interrupted admissions, disables the old generation and queues a new snapshot. It cannot recover deleted accounts. Do not clear a fence directly in SQL.

Account export includes `data/analytics.json`: private board/chart/query/editor/dataset definitions, calculated columns/metrics and the default/region mapping. Connection credentials, result caches and session digests are excluded. Account deletion immediately revokes source sessions and fences reads. The durable registry survives account deletion; cleanup terminates the exact owner's work, drops its login/projected rows and removes Superset objects/identity. Only verified cleanup across stores allows the existing deletion receipt to become complete. Failed cleanup remains retryable. Superset serializes integration mutations per owner and commits a permanent hash-only deletion tombstone, so a timed-out provisioner cannot recreate deleted metadata.

Back up the application database, originals, deletion ledger, analytics database, Superset metadata and server secrets together. Preserve the deletion ledger independently of database backup age. During restore, keep app/worker/gateway traffic stopped, restore all databases/volumes/secrets, then run `python -m backend.app.account_data --restore` in the configured application image. This force-replays the ledger, drains cleanup, and verifies primary rows, originals, projection rows/logins/requests and Superset identities/connections. A failed verification blocks reopening traffic. Restore an old Superset backup only while traffic is fenced; replay must remove tombstoned objects before serving it.

For a Superset upgrade, back up metadata and rehearse on disposable copies first. Run native `superset db upgrade` and `superset init`; repeat both pilot privacy/bounds/browser suites and the application handoff/export/delete/restore suites on the candidate image. Review `security.py`, `editor_state.py`, `query_boundary.py` and metadata cleanup against that exact release. Rebuild with the previous pinned digest and restore its metadata backup to roll back. Do not automatically upgrade the image tag.

Application acceptance is still in progress; measured capacity, restore and upgrade reports must be recorded before D47 is marked verified.
