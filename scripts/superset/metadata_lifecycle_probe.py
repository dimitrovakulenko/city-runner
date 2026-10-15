"""Run inside Superset against an already tombstoned synthetic owner only."""

import json
import os

from superset.app import create_app
from sqlalchemy import text

from services.analytics.superset_rpc import owner_rpc


def main():
    if os.environ.get("CITY_RUNNER_SYNTHETIC_ACCEPTANCE") != "1":
        raise RuntimeError("Explicit disposable synthetic acceptance mode is required.")
    app = create_app()
    from superset import db
    from superset.models.core import Database
    from superset.connectors.sqla.models import SqlaTable

    with app.app_context():
        owner = db.session.execute(text("SELECT owner_key FROM city_runner_deleted_owners ORDER BY owner_key LIMIT 1")).scalar_one()
        headers = {"X-Analytics-Secret": os.environ["ANALYTICS_SECRET"]}
        client = app.test_client()
        call = lambda action: client.post(f"/integration/{owner}/{action}", headers=headers, json={})
        checks = {}
        checks["deleted_owner_cannot_be_reprovisioned"] = call("provision").status_code == 404
        with owner_rpc(owner, "verify-deleted"):
            checks["inflight_handler_serializes_cleanup"] = call("delete").status_code == 503
        assert call("verify-deleted").json["deleted"]
        # Reproduce a restored/interrupted metadata connection with no user.
        database = Database(database_name=owner, sqlalchemy_uri="postgresql://analytics-postgres/analytics")
        db.session.add(database)
        db.session.flush()
        db.session.add(SqlaTable(database=database, schema="analytics", table_name="synthetic_orphan"))
        db.session.commit()
        checks["orphan_connection_detected"] = not call("verify-deleted").json["deleted"]
        checks["orphan_cleanup_completed"] = call("delete").json["complete"]
        checks["orphan_connection_removed"] = call("verify-deleted").json["deleted"]
        checks["tombstone_still_blocks_provision"] = call("provision").status_code == 404
        print(json.dumps({"checks": checks, "probe_passed": all(checks.values())}, indent=2))
        if not all(checks.values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
