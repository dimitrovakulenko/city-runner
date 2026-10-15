"""Synthetic native definitions retained across the pinned 6.0-to-6.1 migration."""

import json
import sys

from superset.app import create_app
from superset import db, security_manager

with create_app().app_context():
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.models.sql_lab import SavedQuery

    for name in ("pilot-a", "pilot-b"):
        user = security_manager.find_user(username=name)
        database = db.session.query(Database).filter_by(database_name=name).one()
        dataset = db.session.query(SqlaTable).filter_by(database_id=database.id, table_name="activities").one()
        marker = "Previous release " + name
        if sys.argv[1] == "seed":
            chart = Slice(slice_name=marker, viz_type="table", datasource_id=dataset.id, datasource_type="table",
                params=json.dumps({"viz_type": "table", "query_mode": "raw", "all_columns": ["activity_id", "distance_m"], "row_limit": 100}), owners=[user])
            db.session.add(chart)
            db.session.add(Dashboard(dashboard_title=marker, owners=[user], slices=[chart], published=False))
            db.session.add(SavedQuery(user_id=user.id, db_id=database.id, label=marker, schema="analytics", sql="SELECT activity_id,distance_m FROM analytics.activities"))
            db.session.commit()
        else:
            chart = db.session.query(Slice).filter_by(slice_name=marker).one()
            board = db.session.query(Dashboard).filter_by(dashboard_title=marker).one()
            query = db.session.query(SavedQuery).filter_by(label=marker).one()
            assert chart.owners == [user] and board.owners == [user] and board.slices == [chart]
            assert chart.datasource_id == dataset.id and query.user_id == user.id and query.db_id == database.id
            assert query.sql == "SELECT activity_id,distance_m FROM analytics.activities"
            assert not board.published and database.allow_dml is False and database.allow_run_async is False
    print("Previous-release private definitions and owner-bound connections verified.")
