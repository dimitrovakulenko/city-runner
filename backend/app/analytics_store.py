"""Owner roles and scalar projections in a separate PostgreSQL instance."""

from __future__ import annotations

import hashlib
import hmac
import re
import time

from sqlalchemy import (BigInteger, Boolean, Column, Date, Float, Index, Integer,
                        MetaData, String, Table, Text, delete, insert, text)


metadata = MetaData(schema="analytics")


def _table(name, fields):
    table = Table(name, metadata, Column("owner_key", String(64), nullable=False),
                  *(Column(field, kind) for field, kind in fields))
    Index(f"ix_{name}_owner", table.c.owner_key)
    return table


TABLES = {
    "activities": _table("activities", [
        ("activity_id", BigInteger), ("name", Text), ("recorded_date", Date),
        ("activity_type", Text), ("distance_m", Float), ("duration_s", Float),
        ("pace_s_per_km", Float), ("statistics_version", Integer), ("has_gpx", Boolean), ("has_fit", Boolean)]),
    "dataset_status": _table("dataset_status", [
        ("dataset_id", BigInteger), ("dataset_state", Text), ("coverage_status", Text),
        ("progress_revision", Text), ("pending_sources", Integer), ("failed_sources", Integer), ("pending_imports", Integer)]),
    "street_progress": _table("street_progress", [
        ("dataset_id", BigInteger), ("street_id", BigInteger), ("city_id", BigInteger),
        ("street_name", Text), ("city_name", Text), ("rule", Text), ("eligible_nodes", Integer),
        ("threshold_nodes", Integer), ("visited_nodes", Integer), ("manual_complete", Boolean),
        ("gps_complete", Boolean), ("effective_complete", Boolean)]),
    "exploration_history": _table("exploration_history", [
        ("activity_id", BigInteger), ("dataset_id", BigInteger), ("rule", Text), ("recorded_date", Date),
        ("history_status", Text), ("same_day_order_approximate", Boolean), ("supported_nodes", Integer),
        ("new_nodes", Integer), ("streets_advanced", Integer), ("streets_completed", Integer)]),
    "activity_nodes": _table("activity_nodes", [
        ("activity_id", BigInteger), ("dataset_id", BigInteger), ("node_token", String(64)),
        ("recorded_date", Date), ("new_with_activity", Boolean)]),
    "activity_street_nodes": _table("activity_street_nodes", [
        ("activity_id", BigInteger), ("dataset_id", BigInteger), ("street_id", BigInteger),
        ("node_token", String(64)), ("recorded_date", Date), ("new_with_activity", Boolean)]),
}


def owner_key(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()


def role_name(owner: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", owner) is None:
        raise ValueError("Invalid analytics owner.")
    return "cr_a_" + owner[:48]


def role_password(secret: bytes, owner: str) -> str:
    return hmac.new(secret, f"analytics-role:{owner}".encode(), hashlib.sha256).hexdigest()


def initialize_store(engine) -> None:
    """Operator-only initialization; never point this at the application instance."""
    with engine.begin() as db:
        if db.exec_driver_sql("SELECT to_regclass('public.activities') IS NOT NULL").scalar_one():
            raise RuntimeError("Analytics requires a separate database from application tables.")
        db.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS analytics")
        db.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS control")
        db.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        database = db.dialect.identifier_preparer.quote(db.exec_driver_sql("SELECT current_database()").scalar_one())
        db.exec_driver_sql(f"REVOKE CREATE,TEMPORARY ON DATABASE {database} FROM PUBLIC")
        db.exec_driver_sql("REVOKE ALL ON SCHEMA control FROM PUBLIC")
        db.exec_driver_sql("""CREATE TABLE IF NOT EXISTS control.schema_version (
            singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),version integer NOT NULL)""")
        db.exec_driver_sql("INSERT INTO control.schema_version VALUES (true,1) ON CONFLICT DO NOTHING")
        if db.exec_driver_sql("SELECT version FROM control.schema_version").scalar_one() != 1:
            raise RuntimeError("Unsupported analytics store schema version.")
        db.exec_driver_sql("""CREATE TABLE IF NOT EXISTS control.owners (
            owner_key char(64) PRIMARY KEY,role_name name NOT NULL UNIQUE,
            generation uuid,source_revision text,fenced boolean NOT NULL DEFAULT false,
            enabled boolean NOT NULL DEFAULT false)""")
        metadata.create_all(db)
        for table in TABLES.values():
            db.exec_driver_sql(f"ALTER TABLE analytics.{table.name} ENABLE ROW LEVEL SECURITY")
            db.exec_driver_sql(f"REVOKE ALL ON analytics.{table.name} FROM PUBLIC")


def provision_role(db, owner: str, secret: bytes) -> str:
    role = role_name(owner)
    db.execute(text("""INSERT INTO control.owners(owner_key,role_name) VALUES (:owner,:role)
        ON CONFLICT DO NOTHING"""), {"owner": owner, "role": role})
    db.execute(text("SELECT owner_key FROM control.owners WHERE owner_key=:owner FOR UPDATE"), {"owner": owner}).one()
    existing = db.execute(text("""SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
        OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) AS privileged
        FROM pg_roles WHERE rolname=:role"""), {"role": role}).mappings().first()
    if existing:
        if existing["privileged"]:
            raise RuntimeError("Analytics role has unexpected privileges.")
        return role
    password = role_password(secret, owner)
    db.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' CONNECTION LIMIT 3")
    db.exec_driver_sql(f"ALTER ROLE {role} SET default_transaction_read_only=on")
    db.exec_driver_sql(f"ALTER ROLE {role} SET statement_timeout='1500ms'")
    db.exec_driver_sql(f"ALTER ROLE {role} SET temp_file_limit='16MB'")
    db.exec_driver_sql(f"GRANT USAGE ON SCHEMA analytics TO {role}")
    for table in TABLES.values():
        db.exec_driver_sql(f"DROP POLICY IF EXISTS {role} ON analytics.{table.name}")
        db.exec_driver_sql(f"CREATE POLICY {role} ON analytics.{table.name} FOR SELECT TO {role} USING (owner_key='{owner}')")
        db.exec_driver_sql(f"GRANT SELECT ON analytics.{table.name} TO {role}")
    return role


def stage_snapshot(db, owner: str, snapshot, deadline: float) -> None:
    """Caller holds control.owners row lock; commit only after source revalidation."""
    role_name(owner)
    if set(snapshot.tables) != set(TABLES):
        raise ValueError("Unexpected analytics snapshot tables.")
    for name, table in TABLES.items():
        db.execute(delete(table).where(table.c.owner_key == owner))
        rows = snapshot.tables[name]
        for start in range(0, len(rows), 1000):
            remaining = min(1500, int((deadline - time.monotonic()) * 1000))
            if remaining <= 0:
                raise RuntimeError("Analytics publication exceeded its work limit.")
            db.execute(text("SELECT set_config('statement_timeout',:timeout,true)"), {"timeout": f"{remaining}ms"})
            db.execute(insert(table), [{**row, "owner_key": owner} for row in rows[start:start + 1000]])


def remove_owner(db, owner: str) -> None:
    role = role_name(owner)
    db.execute(text("SELECT owner_key FROM control.owners WHERE owner_key=:owner FOR UPDATE"), {"owner": owner}).first()
    db.execute(text("UPDATE control.owners SET enabled=false,generation=NULL WHERE owner_key=:owner"), {"owner": owner})
    if db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}).first():
        db.exec_driver_sql(f"ALTER ROLE {role} NOLOGIN")
        db.execute(text("SELECT pg_terminate_backend(pid,1000) FROM pg_stat_activity WHERE usename=:role"), {"role": role}).all()
        if db.execute(text("SELECT 1 FROM pg_stat_activity WHERE usename=:role"), {"role": role}).first():
            raise RuntimeError("Analytics owner connections remain active.")
        for table in TABLES.values():
            db.exec_driver_sql(f"DROP POLICY IF EXISTS {role} ON analytics.{table.name}")
            db.exec_driver_sql(f"REVOKE ALL ON analytics.{table.name} FROM {role}")
        db.exec_driver_sql(f"REVOKE USAGE ON SCHEMA analytics FROM {role}")
        db.exec_driver_sql(f"DROP ROLE {role}")
    for table in TABLES.values():
        db.execute(delete(table).where(table.c.owner_key == owner))
