"""Bounded scalar snapshots for the isolated personal analytics database."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import hmac
import time

from fastapi import HTTPException
from sqlalchemy import text

from backend.app.activity_impact import MAX_HISTORY_ACTIVITIES, _QUALIFIED_SOURCES, _calendar_date
from backend.app.coverage import track_may_touch_dataset_sql
from backend.app.explorer import _dataset
from backend.app.owner_guard import owner_lock
from backend.app.sync_status import _read_version

MAX_STREETS = 10_000
MAX_SUPPORT_ROWS = 200_000
SNAPSHOT_SECONDS = 30


@dataclass
class AnalyticsSnapshot:
    revision: str
    tables: dict[str, list[dict]]


def read_snapshot(engine, account_id: str, dataset_id: int | None, secret: bytes,
                  *, deadline: float | None = None) -> AnalyticsSnapshot:
    """Read one MVCC version; publication must recheck revision under an owner lock."""
    if len(secret) < 32:
        raise ValueError("Analytics requires a server secret of at least 32 bytes.")
    deadline = deadline or time.monotonic() + SNAPSHOT_SECONDS
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db, db.begin():
        db.exec_driver_sql("SET TRANSACTION READ ONLY")
        db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
        owner_lock(db, account_id)
        revision = _read_version(db, account_id, deadline)

        def rows(sql, limit, **params):
            remaining = min(1500, int((deadline - time.monotonic()) * 1000))
            if remaining <= 0:
                raise HTTPException(503, "Analytics snapshot exceeded its work limit.")
            db.execute(text("SELECT set_config('statement_timeout',:timeout,true)"), {
                "timeout": f"{remaining}ms",
            })
            result = db.execute(text(sql), {"account_id": account_id, "dataset_id": dataset_id,
                                           "limit": limit + 1, **params}).mappings().fetchmany(limit + 1)
            if len(result) > limit:
                raise HTTPException(503, "Analytics snapshot exceeds its input limit; no partial totals published.")
            return [dict(row) for row in result]

        activities = rows(_ACTIVITIES, MAX_HISTORY_ACTIVITIES)
        for activity in activities:
            activity["recorded_date"] = _calendar_date(activity.pop("date"))
        tables = {"activities": activities, "dataset_status": [], "street_progress": [],
                  "exploration_history": [], "activity_nodes": [], "activity_street_nodes": []}
        if dataset_id is None:
            tables["dataset_status"] = [{"dataset_id": None, "dataset_state": None,
                "coverage_status": "missing-geography", "progress_revision": None,
                "pending_sources": None, "failed_sources": None, "pending_imports": None}]
            return AnalyticsSnapshot(revision, tables)

        dataset = _dataset(db, dataset_id)
        readiness = rows(_READINESS, 1)[0]
        coverage_status = ("pending" if dataset["state"] == "importing" or dataset["coverage_mode"] != "complete"
                           else "failed" if readiness["failed_sources"] else
                           "pending" if readiness["pending_sources"] else "ready")
        tables["dataset_status"] = [{"dataset_id": dataset_id, "dataset_state": dataset["state"],
            "progress_revision": revision, **readiness,
            "coverage_status": coverage_status}]
        streets = rows(_STREETS, MAX_STREETS)
        qualified = _QUALIFIED_SOURCES + " AND " + _FILE_SOURCE
        provenance = {row["activity_id"] for row in rows(
            f"WITH qualified AS ({qualified}) SELECT DISTINCT activity_id FROM qualified LIMIT :limit",
            MAX_HISTORY_ACTIVITIES)}
        supports = rows(_SUPPORT.replace("{{qualified}}", qualified), MAX_SUPPORT_ROWS)
        allowed = {activity["activity_id"]: activity for activity in activities}
        supports = [row for row in supports if row["activity_id"] in allowed]
        # Only contributing unknown dates affect every chronological attribution.
        unknown_history = any(allowed[row["activity_id"]]["recorded_date"] is None for row in supports)
        ready = coverage_status == "ready"
        first = {}
        if not unknown_history:
            for row in supports:
                activity = allowed[row["activity_id"]]
                event = (activity["recorded_date"], activity["activity_id"])
                node = row["node_id"]
                first[node] = min(first.get(node, event), event)

        # Keep only opaque visited-node equality tokens, never coordinates, raw
        # OSM node IDs or an unvisited geography index. These permit period unions.
        tokens = {row["node_id"]: hmac.new(secret,
            f"{account_id}:{dataset_id}:{row['node_id']}".encode(), hashlib.sha256).hexdigest()
            for row in supports}
        by_activity = defaultdict(set)
        for index, row in enumerate(supports):
            if index % 1024 == 0 and time.monotonic() >= deadline:
                raise HTTPException(503, "Analytics snapshot exceeded its work limit.")
            identifier, node = row["activity_id"], row["node_id"]
            by_activity[identifier].add(node)
            if ready:
                tables["activity_nodes"].append({"dataset_id": dataset_id, "activity_id": identifier,
                    "node_token": tokens[node], "recorded_date": allowed[identifier]["recorded_date"],
                    "new_with_activity": None if unknown_history else first[node][1] == identifier})

        street_support = rows(_STREET_SUPPORT.replace("{{qualified}}", qualified), MAX_SUPPORT_ROWS)
        street_nodes = defaultdict(set)
        advanced = defaultdict(set)
        for row in street_support:
            identifier, node, street = row["activity_id"], row["node_id"], row["street_id"]
            if identifier not in allowed:
                continue
            street_nodes[street].add(node)
            is_new = None if unknown_history else first[node][1] == identifier
            if is_new:
                advanced[identifier].add(street)
            if ready:
                tables["activity_street_nodes"].append({"dataset_id": dataset_id, "street_id": street,
                    "activity_id": identifier, "node_token": tokens[node],
                    "recorded_date": allowed[identifier]["recorded_date"], "new_with_activity": is_new})

        completed = defaultdict(int)
        for rule in ("normal", "strict"):
            for street in streets:
                eligible = street["eligible_nodes"]
                threshold = eligible if rule == "strict" or eligible < 10 else (eligible * 9 + 9) // 10
                nodes = street_nodes[street["street_id"]]
                gps_complete = len(nodes) >= threshold if ready else None
                tables["street_progress"].append({**street, "dataset_id": dataset_id, "rule": rule,
                    "eligible_nodes": eligible if ready else None,
                    "threshold_nodes": threshold if ready else None, "visited_nodes": len(nodes) if ready else None,
                    "gps_complete": gps_complete,
                    "effective_complete": street["manual_complete"] or gps_complete})
                if ready and not unknown_history and len(nodes) >= threshold:
                    crossing = sorted(first[node] for node in nodes)[threshold - 1]
                    completed[(rule, crossing[1])] += 1
            for activity in activities:
                identifier = activity["activity_id"]
                historical = not unknown_history and activity["recorded_date"] is not None
                status = ("missing-provenance" if identifier not in provenance else
                          "unknown-dates" if not historical else "ready")
                available = ready and identifier in provenance
                tables["exploration_history"].append({"activity_id": identifier,
                    "dataset_id": dataset_id, "rule": rule, "recorded_date": activity["recorded_date"],
                    "history_status": status, "same_day_order_approximate": True,
                    "supported_nodes": len(by_activity[identifier]) if available else None,
                    "new_nodes": sum(first[node][1] == identifier for node in by_activity[identifier])
                        if available and historical else None,
                    "streets_advanced": len(advanced[identifier]) if available and historical else None,
                    "streets_completed": completed[(rule, identifier)] if available and historical else None})
        if time.monotonic() >= deadline:
            raise HTTPException(503, "Analytics snapshot exceeded its work limit.")
        return AnalyticsSnapshot(revision, tables)


_FILE_SOURCE = """source.source_kind IN ('gpx','fit') AND NOT EXISTS (
    SELECT 1 FROM activity_sources excluded WHERE excluded.account_id=source.account_id
      AND excluded.activity_id=source.activity_id AND excluded.source_kind NOT IN ('gpx','fit'))"""

_READINESS = """
    WITH current_sources AS (
      SELECT run.status AS run_status,job.status AS job_status
      FROM activity_sources source
      JOIN activities activity ON activity.user_id=source.account_id AND activity.id=source.activity_id
      LEFT JOIN coverage_source_runs run ON run.account_id=source.account_id AND run.source_id=source.id
        AND run.source_revision=source.revision AND run.dataset_id=:dataset_id
      LEFT JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id
      LEFT JOIN source_dataset_applicability applicability ON applicability.account_id=source.account_id
        AND applicability.source_id=source.id AND applicability.source_revision=source.revision
        AND applicability.dataset_id=:dataset_id
      WHERE source.account_id=:account_id AND source.status='succeeded' AND ({{file_source}})
        AND (applicability.applicable IS TRUE OR
          (applicability.applicable IS NULL AND {{candidate}}))
    )
    SELECT count(*) FILTER (WHERE run_status='failed' OR job_status IN ('failed','cancelled')) AS failed_sources,
      count(*) FILTER (WHERE run_status IS NULL OR run_status<>'succeeded'
        OR job_status IS DISTINCT FROM 'succeeded') AS pending_sources,
      (SELECT count(*) FROM activity_sources source WHERE source.account_id=:account_id
        AND source.status IN ('queued','processing') AND ({{file_source}})) AS pending_imports
    FROM current_sources LIMIT :limit
""".replace("{{file_source}}", _FILE_SOURCE).replace("{{candidate}}", track_may_touch_dataset_sql(
    "activity.track_geometry", ":dataset_id"))

_ACTIVITIES = """
    SELECT activity.id AS activity_id,activity.name,activity.date,activity.activity_type,
      activity.distance_m,activity.duration_s,activity.pace_s_per_km,activity.statistics_version,
      EXISTS (SELECT 1 FROM activity_sources s WHERE s.account_id=activity.user_id
        AND s.activity_id=activity.id AND s.status='succeeded' AND s.source_kind='gpx') AS has_gpx,
      EXISTS (SELECT 1 FROM activity_sources s WHERE s.account_id=activity.user_id
        AND s.activity_id=activity.id AND s.status='succeeded' AND s.source_kind='fit') AS has_fit
    FROM activities activity WHERE activity.user_id=:account_id
      AND EXISTS (SELECT 1 FROM activity_sources s WHERE s.account_id=activity.user_id
        AND s.activity_id=activity.id AND s.status='succeeded' AND s.source_kind IN ('gpx','fit'))
      AND NOT EXISTS (SELECT 1 FROM activity_sources s WHERE s.account_id=activity.user_id
        AND s.activity_id=activity.id AND s.source_kind NOT IN ('gpx','fit'))
    ORDER BY activity.id LIMIT :limit
"""

_STREETS = """
    SELECT street.id AS street_id,street.city_id,street.display_name AS street_name,
      city.name AS city_name,street.eligible_node_count AS eligible_nodes,
      (manual.street_id IS NOT NULL) AS manual_complete
    FROM streets street JOIN cities city ON city.dataset_id=street.dataset_id AND city.id=street.city_id
    LEFT JOIN manual_street_completions manual ON manual.account_id=:account_id
      AND manual.dataset_id=street.dataset_id AND manual.street_id=street.id
    WHERE street.dataset_id=:dataset_id AND street.eligible_node_count>0
    ORDER BY street.id LIMIT :limit
"""

_SUPPORT = """
    WITH qualified AS ({{qualified}})
    SELECT DISTINCT qualified.activity_id,contribution.node_id
    FROM qualified JOIN source_node_contributions contribution
      ON contribution.account_id=qualified.account_id AND contribution.source_id=qualified.source_id
      AND contribution.source_revision=qualified.revision AND contribution.dataset_id=:dataset_id
    ORDER BY qualified.activity_id,contribution.node_id LIMIT :limit
"""

_STREET_SUPPORT = """
    WITH qualified AS ({{qualified}})
    SELECT DISTINCT qualified.activity_id,contribution.node_id,street_node.street_id
    FROM qualified JOIN source_node_contributions contribution
      ON contribution.account_id=qualified.account_id AND contribution.source_id=qualified.source_id
      AND contribution.source_revision=qualified.revision AND contribution.dataset_id=:dataset_id
    JOIN street_nodes street_node ON street_node.dataset_id=contribution.dataset_id
      AND street_node.osm_node_id=contribution.node_id
    JOIN streets street ON street.dataset_id=street_node.dataset_id AND street.id=street_node.street_id
      AND street.eligible_node_count>0
    ORDER BY qualified.activity_id,contribution.node_id,street_node.street_id LIMIT :limit
"""
