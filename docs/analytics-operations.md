# Personal analytics operations

The opt-in local services are in `services/analytics/compose.yaml`. They use the existing application database and original-file/deletion-ledger volume, plus an isolated PostgreSQL instance containing the scalar analytics projection and Superset metadata. Superset 6.1.0 and all base images are pinned by digest. Redis, Celery, asynchronous SQL and query/result caches are disabled. Native temporary filter/Explore form state remains in the Superset metadata store with private object access checks. Account cleanup deletes those payloads, including after backup restore. Cloud installation remains D34.

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

There is one active gateway instance, enforced by a durable 30-second database lease. Local counters permit two SQL requests per owner/four globally, separately bounded metadata and cancellation work, and preview queues bounded to three seconds within the same overall deadline. Disabled legacy telemetry is rejected before admission and consumes no query slot. Replacement processes reconcile interrupted durable admissions before serving. Running a second gateway cannot multiply the SQL budget. A crash may delay startup for the previous lease to expire. Internal authentication calls admit two active requests and at most 64 including waiters, keeping queue time within the existing RPC deadline and reserving backend database capacity. Static assets still validate access before and after their response; burst admission does not cache access decisions.

Database work has an immutable eight-second deadline, verified PID/backend-start termination, 10,000 fetched rows, 16 MiB spill, a 1 MiB request and 8 MiB dynamic response limit. SQL authors cannot relax the external timer. Container limits bound oversized client buffering; exceeding a limit fails the request instead of publishing partial totals. Boundary failures persist an owner fence. Logs report only a failure category, never SQL text, results or credentials.

## Recovery and deletion

For a boundary failure, inspect service health and the owner's isolated database sessions using operator credentials. The owner key is the SHA-256 of the account ID, available in the primary `analytics_accounts` registry. Run:

```sh
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml exec -T projector python -m backend.app.analytics_worker --recover-owner OWNER_SHA256
docker compose --env-file /private/path/analytics.env -f services/analytics/compose.yaml restart gateway
```

Recovery verifies that owner database work has stopped, clears interrupted admissions, disables the old generation and queues a new snapshot. It cannot recover deleted accounts. Do not clear a fence directly in SQL.

Account export includes `data/analytics.json`: private board/chart/query/editor/dataset definitions, calculated columns/metrics and the default/region mapping. Connection credentials, result caches and session digests are excluded. Account deletion immediately revokes source sessions and fences reads. The durable registry survives account deletion; cleanup terminates the exact owner's work, drops its login/projected rows and removes Superset objects/identity. Only verified cleanup across stores allows the existing deletion receipt to become complete. Failed cleanup remains retryable. Superset serializes integration mutations per owner and commits a permanent hash-only deletion tombstone, so a timed-out provisioner cannot recreate deleted metadata.

Back up the application database, originals, deletion ledger, analytics database, Superset metadata and server secrets together. Preserve the deletion ledger independently of database backup age. During restore, keep app/worker/gateway traffic stopped. Restore the primary database and stable server secrets first. Create the empty isolated analytics/metadata databases with their configured owners, then run `python -m backend.app.analytics_worker --prepare-restore` in the configured application image. This recreates deterministic owner roles needed by the old analytics policies and ACLs, including roles removed by a later account deletion. Restore the analytics dump with its policies/ACLs and restore Superset metadata as the configured metadata role (`pg_restore --role superset_meta --no-owner` in the sample stack). Restore originals, retain the latest deletion ledger, and start Superset only on its internal network so cleanup can reach it. Then run `python -m backend.app.account_data --restore` in the configured application image. This force-replays the ledger, drains cleanup, and verifies primary rows, originals, projection rows/logins/requests and Superset identities/connections. A failed verification blocks reopening traffic. Restore an old Superset backup only while traffic is fenced; replay must remove tombstoned objects before serving it.

For a Superset upgrade, back up metadata and rehearse on disposable copies first. Run native `superset db upgrade` and `superset init`; repeat both pilot privacy/bounds/browser suites and the application handoff/export/delete/restore suites on the candidate image. Review `security.py`, `editor_state.py`, `query_boundary.py`, `patch_sql_lab.py` and metadata cleanup against that exact release. Both current images apply a SHA-256-guarded, one-character fix to the pinned SQL Lab bundle: equal initialization timestamps count as initialized. Without it, native saved-query reopening can remain on its loading screen. The image build fails if the upstream asset changes. Rebuild with the previous pinned digest and restore its metadata backup to roll back. Do not automatically upgrade the image tag.

## Disposable acceptance

Use a dedicated loopback PostGIS admin database, never the application or private PoC database. The harness creates its own empty source database, isolated service project, synthetic originals and secrets; `finally` removes that project, its volumes, the generated database and temporary credentials.

```sh
SUPERSET_PROBE_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:DISPOSABLE_PASSWORD@127.0.0.1:55447/postgres \
  python scripts/superset/application_acceptance.py --capacity --recovery --output /tmp/analytics-acceptance
```

This runs two-owner native browser authoring and session races, a 5,000-activity/five-million-sample mixed SQL/map/import workload, actual query timeout/stop, a killed gateway with durable-admission recovery, API/Superset/worker restart, and backup restore followed by deletion-journal verification. The large account archive is outside the capacity claim; the existing 512 MiB export limit remains explicit.

`upgrade-compose.yaml` layers a pinned 6.0.0 offline initializer over the disposable pilot, populates two private owners' saved queries/charts/boards, then migrates to pinned 6.1.0 and verifies preservation. Run privacy and native browser authoring against the migrated store before removing it:

```sh
docker compose -p city-runner-superset-upgrade -f scripts/superset/compose.yaml -f scripts/superset/upgrade-compose.yaml up -d --build --wait --wait-timeout 240
SUPERSET_PROBE_ORIGIN=http://127.0.0.1:18090 SUPERSET_PROBE_PROJECT=city-runner-superset-upgrade python scripts/superset/probe.py --output /tmp/upgrade-privacy.json
SUPERSET_PROBE_ORIGIN=http://127.0.0.1:18090 node scripts/superset/browser_probe.mjs --output /tmp/upgrade-browser.json
docker compose -p city-runner-superset-upgrade -f scripts/superset/compose.yaml -f scripts/superset/upgrade-compose.yaml down -v
```

Hosted measured evidence is recorded with the verified implementation commit in `docs/benchmarks/d45-*.json`, `d46-*.json` and `d47-*.json`. These synthetic shared-runner measurements do not establish a fleet SLA. Hosted application and upgrade jobs repeat the clean acceptance independently of the original pilot bounds regression.
