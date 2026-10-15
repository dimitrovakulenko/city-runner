"""Private provisioning/export/cleanup RPC; never exposed by the UI proxy."""

import hmac
import json
import os
import re
import time
from contextlib import contextmanager

from flask import abort, g, jsonify, request
from sqlalchemy.engine.url import make_url

from services.analytics.identity import role_name, role_password


def provision(owner):
    from superset import db, security_manager
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.core import Database

    role = security_manager.add_role("Personal-" + owner[:48])
    role.permissions = list({permission for name in ("Gamma", "sql_lab")
        for permission in security_manager.find_role(name).permissions})
    security_manager.add_permission_role(role, security_manager.add_permission_view_menu("can_write", "Dataset"))
    user = security_manager.find_user(username=owner)
    if user is None:
        user = security_manager.add_user(username=owner, first_name="Personal", last_name="analytics",
            email=owner + "@analytics.invalid", role=role)
    user.active = True
    user.roles = [role]
    database = db.session.query(Database).filter_by(database_name=owner).one_or_none()
    if database is None:
        database = Database(database_name=owner)
        db.session.add(database)
    url = make_url(os.environ["ANALYTICS_DATABASE_URL"]).set(username=role_name(owner),
        password=role_password(os.environ["ANALYTICS_SECRET"].encode(), owner))
    database.set_sqlalchemy_uri(url.render_as_string(hide_password=False))
    database.expose_in_sqllab = True
    database.allow_run_async = database.allow_ctas = database.allow_cvas = database.allow_dml = database.allow_file_upload = False
    db.session.commit()
    security_manager.add_permission_role(role, security_manager.add_permission_view_menu("database_access", database.perm))
    for table in ("activities", "dataset_status", "street_progress", "exploration_history", "activity_nodes", "activity_street_nodes"):
        dataset = db.session.query(SqlaTable).filter_by(database_id=database.id, schema="analytics", table_name=table).one_or_none()
        if dataset is None:
            dataset = SqlaTable(database=database, schema="analytics", table_name=table, owners=[user])
            db.session.add(dataset)
            db.session.flush()
            dataset.fetch_metadata()
    db.session.commit()
    return user, database


def state(user):
    from superset import db
    from superset.models.dashboard import Dashboard

    boards = db.session.query(Dashboard).filter(Dashboard.owners.any(id=user.id)).order_by(Dashboard.id).limit(10001).all()
    if len(boards) > 10000:
        abort(503)
    return {"boards": [{"id": board.id, "title": board.dashboard_title} for board in boards]}


def definitions(session, user, database):
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.models.sql_lab import Query, SavedQuery, TabState

    selections = (
        ("dashboards", session.query(Dashboard).filter(Dashboard.owners.any(id=user.id)),
            ("id", "dashboard_title", "position_json", "json_metadata", "css")),
        ("charts", session.query(Slice).filter(Slice.owners.any(id=user.id)),
            ("id", "slice_name", "viz_type", "params", "query_context", "datasource_id", "datasource_type")),
        ("saved_queries", session.query(SavedQuery).filter_by(user_id=user.id),
            ("id", "label", "description", "sql", "schema", "catalog")),
        ("datasets", session.query(SqlaTable).filter_by(database_id=database.id),
            ("id", "table_name", "schema", "sql", "description")),
        ("editor_tabs", session.query(TabState).filter_by(user_id=user.id),
            ("id", "label", "sql", "schema", "catalog", "saved_query_id", "latest_query_id")),
        ("query_history", session.query(Query).filter_by(user_id=user.id),
            ("id", "client_id", "sql", "schema", "catalog", "status")),
    )
    result, size = {}, 0
    for name, query, fields in selections:
        result[name] = []
        for index, entity in enumerate(query.enable_eagerloads(False).yield_per(50)):
            if index >= 10000:
                abort(503)
            row = {field: getattr(entity, field) for field in fields}
            if name == "datasets":
                row["columns"] = [{field: getattr(column, field) for field in
                    ("column_name", "verbose_name", "expression", "type", "is_dttm", "filterable", "groupby", "description")}
                    for column in entity.columns]
                row["metrics"] = [{field: getattr(metric, field) for field in
                    ("metric_name", "expression", "metric_type", "verbose_name", "description", "d3format")}
                    for metric in entity.metrics]
            size += len(json.dumps(row).encode())
            if size > 8 * 1024 * 1024 - 65536:
                abort(503)
            result[name].append(row)
    return result


def export_snapshot(owner):
    from superset import security_manager
    from superset.models.core import Database
    from flask import current_app
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    # Superset's metadata-engine checkout hooks already execute SQL. Establish
    # isolation before checkout using a fresh read-only, unpooled connection.
    engine = create_engine(current_app.config["SQLALCHEMY_DATABASE_URI"], isolation_level="REPEATABLE READ",
        poolclass=NullPool, connect_args={"connect_timeout": 1,
            "options": "-c default_transaction_read_only=on -c statement_timeout=1500"})
    try:
        with engine.connect() as connection, Session(bind=connection, autoflush=False) as session:
            user = session.query(security_manager.user_model).filter_by(username=owner).one_or_none()
            database = session.query(Database).filter_by(database_name=owner).one_or_none()
            if user is None and database is None:
                return {name: [] for name in ("dashboards", "charts", "datasets", "saved_queries", "editor_tabs", "query_history")}
            if user is None or not user.active or database is None:
                abort(503)
            return definitions(session, user, database)
    finally:
        engine.dispose()


def delete_batch(user, database, owner):
    from superset import db, security_manager
    from superset.connectors.sqla.models import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice
    from superset.models.sql_lab import Query, SavedQuery, TabState

    if user:
        user.active = False
        db.session.commit()
    from superset.models.user_attributes import UserAttribute

    selections = [db.session.query(UserAttribute).filter_by(user_id=user.id),
        db.session.query(TabState).filter_by(user_id=user.id),
        db.session.query(Dashboard).filter(Dashboard.owners.any(id=user.id)),
        db.session.query(Slice).filter(Slice.owners.any(id=user.id)),
        db.session.query(SavedQuery).filter_by(user_id=user.id),
        db.session.query(Query).filter_by(user_id=user.id)] if user else []
    if database:
        selections.append(db.session.query(SqlaTable).filter_by(database_id=database.id))
    remaining = False
    budget = 25
    for query in selections:
        objects = query.limit(budget + 1).all()
        for entity in objects[:budget]:
            db.session.delete(entity)
        remaining |= len(objects) > budget
        budget -= min(len(objects), budget)
        if not budget:
            remaining = True
            break
    db.session.commit()
    if remaining:
        return {"complete": False}
    if database:
        db.session.delete(database)
        db.session.commit()
    # Audit/favorite/log FKs vary by Superset release. Remove all remaining
    # user references before deleting the private identity; exercised on 6.1.0.
    if user:
        _remove_user_references(user.id)
        db.session.delete(user)
    role = security_manager.find_role("Personal-" + owner[:48])
    if role:
        db.session.delete(role)
    db.session.commit()
    return {"complete": True}


@contextmanager
def owner_rpc(owner, action):
    from superset import db
    from sqlalchemy import text

    # A separate session lock survives the handler's metadata commits. A client
    # timeout cannot release it while provisioning is still running.
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        key = {"owner": owner}
        deadline = time.monotonic() + 0.5
        while not connection.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:owner,7319))"), key).scalar():
            if time.monotonic() >= deadline:
                abort(503)
            time.sleep(0.02)
        try:
            if action == "delete":
                connection.execute(text("INSERT INTO city_runner_deleted_owners(owner_key) VALUES(:owner) ON CONFLICT DO NOTHING"), key)
            elif action != "verify-deleted" and connection.execute(text(
                    "SELECT 1 FROM city_runner_deleted_owners WHERE owner_key=:owner"), key).first():
                abort(404)
            yield
        finally:
            try:
                if not connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:owner,7319))"), key).scalar():
                    raise RuntimeError("Analytics metadata owner lock was lost.")
            except BaseException:
                connection.invalidate()
                raise


def _remove_user_references(user_id):
    from superset import db
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    # These are metadata references to this exact private user, never shared
    # object IDs. Nullable audit columns are cleared; private join/log rows go.
    for table in inspector.get_table_names():
        if table == "ab_user":
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table)}
        for key in inspector.get_foreign_keys(table):
            if key.get("referred_table") != "ab_user" or len(key["constrained_columns"]) != 1:
                continue
            column = key["constrained_columns"][0]
            quote = db.engine.dialect.identifier_preparer.quote
            table_sql, column_sql = quote(table), quote(column)
            if column in {"created_by_fk", "changed_by_fk"} and columns[column]["nullable"]:
                db.session.execute(text(f"UPDATE {table_sql} SET {column_sql}=NULL WHERE {column_sql}=:user"), {"user": user_id})
            else:
                db.session.execute(text(f"DELETE FROM {table_sql} WHERE {column_sql}=:user"), {"user": user_id})


def register_rpc(app):
    from superset import db, security_manager
    from superset.extensions import csrf
    from superset.models.core import Database
    from query_boundary import REQUEST_SCOPE

    with app.app_context(), db.engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS city_runner_deleted_owners(owner_key char(64) PRIMARY KEY)")

    def handle(owner, action):
        secret = request.headers.get("X-Analytics-Secret", "").encode()
        if not hmac.compare_digest(secret, os.environ["ANALYTICS_SECRET"].encode()):
            abort(401)
        if re.fullmatch(r"[0-9a-f]{64}", owner) is None:
            abort(404)
        token = REQUEST_SCOPE.set((owner, time.monotonic() + 8))
        try:
            with owner_rpc(owner, action):
                return dispatch(owner, action)
        finally:
            REQUEST_SCOPE.reset(token)

    def dispatch(owner, action):
        if action == "export":
            return jsonify(export_snapshot(owner))
        user = security_manager.find_user(username=owner)
        database = db.session.query(Database).filter_by(database_name=owner).one_or_none()
        if action == "provision":
            user, database = provision(owner)
            from services.analytics.templates import initialize_starters

            initialize_starters(user, database)
            return jsonify({"ready": True})
        if action == "delete":
            return jsonify(delete_batch(user, database, owner))
        if action == "verify-deleted":
            return jsonify({"deleted": user is None and database is None
                and security_manager.find_role("Personal-" + owner[:48]) is None})
        if user is None or not user.active or database is None:
            abort(404)
        g.user = user
        if action == "state":
            return jsonify(state(user))
        if action == "template":
            from services.analytics.templates import copy_template

            result = copy_template(user, database, (request.get_json(silent=True) or {}).get("template"))
            db.session.commit()
            return jsonify(result)
        abort(404)

    csrf.exempt(handle)
    app.add_url_rule("/integration/<owner>/<action>", "analytics_internal", handle, methods=["POST"])
