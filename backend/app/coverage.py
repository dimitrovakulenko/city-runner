"""Lease-fenced, sample-to-node coverage matching for versioned OSM data."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from backend.app.jobs import complete, enqueue, fail
from backend.app.owner_guard import owner_lock


DEFAULT_BATCH_SIZE = 1000
MAX_BATCH_SIZE = 5000
MAX_REQUEUE_SOURCES = 10_000
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _samples_from_tracks(tracks: Any) -> list[list[float]]:
    if not isinstance(tracks, list):
        raise ValueError("invalid_activity_tracks")
    result: list[list[float]] = []
    for segment in tracks:
        if not isinstance(segment, list):
            raise ValueError("invalid_activity_tracks")
        for point in segment:
            if (not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)):
                raise ValueError("invalid_activity_tracks")
            lon, lat = float(point[0]), float(point[1])
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("invalid_activity_tracks")
            result.append([lon, lat])
    return result


def _points_json(points: list[list[float]]) -> str:
    return json.dumps(points, allow_nan=False, separators=(",", ":"))


def _count_unsupported_samples(db: Connection, points: list[list[float]]) -> int:
    """Count samples outside the union of active complete city boundaries once."""
    if not points:
        return 0
    return db.execute(text("""WITH samples AS (
            SELECT ST_SetSRID(ST_MakePoint((point->>0)::double precision,
                (point->>1)::double precision), 4326) AS geom
            FROM jsonb_array_elements(CAST(:points AS jsonb)) AS item(point)
        )
        SELECT count(*) FROM samples sample
        WHERE NOT EXISTS (
            SELECT 1 FROM cities city JOIN map_datasets dataset ON dataset.id=city.dataset_id
            WHERE dataset.status='active' AND dataset.coverage_mode='complete'
              AND city.boundary && sample.geom AND ST_Covers(city.boundary, sample.geom)
        )"""), {"points": _points_json(points)}).scalar_one()


def _count_dataset_supported_samples(db: Connection, dataset_id: int,
                                     points: list[list[float]]) -> int:
    if not points:
        return 0
    return db.execute(text("""WITH samples AS (
            SELECT ST_SetSRID(ST_MakePoint((point->>0)::double precision,
                (point->>1)::double precision), 4326) AS geom
            FROM jsonb_array_elements(CAST(:points AS jsonb)) AS item(point)
        )
        SELECT count(*) FROM samples sample
        WHERE EXISTS (
            SELECT 1 FROM cities city WHERE city.dataset_id=:dataset_id
              AND city.boundary && sample.geom AND ST_Covers(city.boundary, sample.geom)
        )"""), {"points": _points_json(points), "dataset_id": dataset_id}).scalar_one()


def _insert_batch(db: Connection, *, account_id: str, source_id: int, source_revision: int,
                  dataset_id: int, points: list[list[float]]) -> None:
    if not points:
        return
    db.execute(text("""WITH samples AS (
            SELECT ST_SetSRID(ST_MakePoint((point->>0)::double precision,
                (point->>1)::double precision), 4326)::geography AS position
            FROM jsonb_array_elements(CAST(:points AS jsonb)) AS item(point)
        ), matches AS (
            SELECT DISTINCT nearby.osm_node_id
            FROM samples sample
            CROSS JOIN LATERAL (
                SELECT node.osm_node_id FROM osm_nodes node
                WHERE node.dataset_id=:dataset_id AND ST_DWithin(node.point, sample.position, 25.0)
            ) nearby
        )
        INSERT INTO source_node_contributions
            (account_id, source_id, source_revision, dataset_id, node_id)
        SELECT :account_id, :source_id, :source_revision, :dataset_id, osm_node_id FROM matches
        ON CONFLICT DO NOTHING"""), {
        "points": _points_json(points), "account_id": account_id, "source_id": source_id,
        "source_revision": source_revision, "dataset_id": dataset_id,
    })


def _pending_summary(db: Connection, account_id: str, dataset_id: int) -> None:
    db.execute(text("""INSERT INTO account_dataset_coverage
            (account_id,dataset_id,status,pending_sources,progress_revision)
        VALUES (:account_id,:dataset_id,'pending',1,1)
        ON CONFLICT (account_id,dataset_id) DO UPDATE SET
            status='pending', pending_sources=GREATEST(account_dataset_coverage.pending_sources,1),
            progress_revision=account_dataset_coverage.progress_revision+1,
            updated_at=clock_timestamp()"""), {"account_id": account_id, "dataset_id": dataset_id})


def queue_source_coverage(db: Connection, *, account_id: str, source_id: int,
                          source_revision: int, activity_id: int,
                          dataset_ids: Iterable[int] | None = None,
                          priority: int = 100) -> dict[str, Any]:
    """Atomically enqueue one idempotent match job per active complete dataset."""
    source = db.execute(text("""SELECT revision FROM activity_sources
        WHERE id=:source_id AND account_id=:account_id AND activity_id=:activity_id
          AND status='succeeded'"""), {
        "source_id": source_id, "account_id": account_id, "activity_id": activity_id,
    }).first()
    if source is None or source[0] != source_revision:
        raise ValueError("source_revision_mismatch")
    tracks = db.execute(text("SELECT tracks FROM activities WHERE id=:id AND user_id=:account_id"), {
        "id": activity_id, "account_id": account_id,
    }).scalar_one()
    points = _samples_from_tracks(tracks)
    if dataset_ids is None:
        datasets = db.execute(text("""SELECT id FROM map_datasets
            WHERE status='active' AND coverage_mode='complete' ORDER BY id""")).scalars().all()
    else:
        datasets = sorted(set(int(value) for value in dataset_ids))
        if datasets:
            available = db.execute(text("""SELECT id FROM map_datasets
                WHERE id=ANY(:ids) AND status IN ('active','importing') AND coverage_mode='complete'"""), {
                "ids": datasets,
            }).scalars().all()
            if sorted(available) != datasets:
                raise ValueError("coverage_dataset_unavailable")

    unmapped_points = _count_unsupported_samples(db, points)
    queued: list[dict[str, Any]] = []
    pending_datasets: list[int] = []
    for dataset_id in datasets:
        key = f"source:{source_id}:r{source_revision}:dataset:{dataset_id}"
        job = enqueue(db, account_id=account_id, kind="match_coverage", dedupe_key=key,
                      payload={"source_id": source_id, "source_revision": source_revision,
                               "dataset_id": dataset_id}, priority=priority)
        run = db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status)
            VALUES (:account_id,:source_id,:source_revision,:dataset_id,:job_id,'queued')
            ON CONFLICT (account_id,source_id,source_revision,dataset_id) DO NOTHING
            RETURNING status"""), {
            "account_id": account_id, "source_id": source_id, "source_revision": source_revision,
            "dataset_id": dataset_id, "job_id": job["id"],
        }).first()
        if run is None:
            existing = db.execute(text("""SELECT run.status, job.status AS job_status
                FROM coverage_source_runs run JOIN jobs job
                  ON job.account_id=run.account_id AND job.id=run.job_id
                WHERE run.account_id=:account_id AND run.source_id=:source_id
                  AND run.source_revision=:source_revision AND run.dataset_id=:dataset_id"""), {
                "account_id": account_id, "source_id": source_id, "source_revision": source_revision,
                "dataset_id": dataset_id,
            }).mappings().one()
            if existing["status"] == "failed" or existing["job_status"] in {"failed", "cancelled"}:
                db.execute(text("""UPDATE jobs SET status='queued', attempts=0,
                        available_at=clock_timestamp(), lease_token=NULL, leased_until=NULL,
                        last_error=NULL, updated_at=clock_timestamp()
                    WHERE account_id=:account_id AND id=:job_id AND status IN ('failed','cancelled')"""), {
                    "account_id": account_id, "job_id": job["id"],
                })
                db.execute(text("""UPDATE coverage_source_runs SET status='queued', last_error=NULL,
                        updated_at=clock_timestamp()
                    WHERE account_id=:account_id AND source_id=:source_id
                      AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
                    "account_id": account_id, "source_id": source_id,
                    "source_revision": source_revision, "dataset_id": dataset_id,
                })
            elif existing["status"] == "succeeded" and existing["job_status"] == "succeeded":
                continue
        pending_datasets.append(dataset_id)
        queued.append({"dataset_id": dataset_id, "job_id": str(job["id"])})
    _refresh_activity_processing(db, account_id=account_id, source_id=source_id,
                                 source_revision=source_revision, activity_id=activity_id)
    # Match workers lock the activity before the account summary. Keep this order
    # when queueing so a requeue never holds the summary while waiting on activity.
    for dataset_id in pending_datasets:
        _pending_summary(db, account_id, dataset_id)
    return {"dataset_ids": list(datasets), "jobs": queued,
            "sample_count": len(points), "unmapped_points": unmapped_points}


def _live_summary(db: Connection, account_id: str, dataset_id: int) -> dict[str, Any]:
    dataset = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id"), {
        "id": dataset_id,
    }).mappings().first()
    if dataset is None:
        return {"status": "pending", "dataset_id": str(dataset_id), "pending_sources": 0,
                "failed_sources": 0, "visited_node_count": None, "unsupported_sample_count": None,
                "progress_revision": 0}
    counts = db.execute(text("""WITH current_sources AS (
            SELECT source.id, source.revision, run.status AS run_status, job.status AS job_status,
                   run.unsupported_sample_count
            FROM activity_sources source
            LEFT JOIN coverage_source_runs run ON run.account_id=source.account_id
                AND run.source_id=source.id AND run.source_revision=source.revision
                AND run.dataset_id=:dataset_id
            LEFT JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id
            WHERE source.account_id=:account_id AND source.status='succeeded'
              AND source.activity_id IS NOT NULL
        )
        SELECT count(*) FILTER (WHERE run_status='failed' OR job_status IN ('failed','cancelled')) AS failed,
               count(*) FILTER (WHERE run_status IS NULL OR run_status<>'succeeded'
                    OR job_status IS DISTINCT FROM 'succeeded') AS pending,
               COALESCE(sum(unsupported_sample_count) FILTER (
                    WHERE run_status='succeeded' AND job_status='succeeded'),0) AS unsupported
        FROM current_sources"""), {"account_id": account_id, "dataset_id": dataset_id}).mappings().one()
    visited = db.execute(text("""SELECT count(DISTINCT contribution.node_id)
        FROM source_node_contributions contribution
        JOIN activity_sources source ON source.account_id=contribution.account_id
            AND source.id=contribution.source_id AND source.revision=contribution.source_revision
        WHERE contribution.account_id=:account_id AND contribution.dataset_id=:dataset_id
          AND source.status='succeeded' AND source.activity_id IS NOT NULL"""), {
        "account_id": account_id, "dataset_id": dataset_id,
    }).scalar_one()
    failed = int(counts["failed"])
    pending = int(counts["pending"])
    if dataset["status"] != "active" or dataset["coverage_mode"] != "complete":
        state = "pending"
    elif failed:
        state = "failed"
    elif pending:
        state = "pending"
    else:
        state = "ready"
    version = db.execute(text("""SELECT progress_revision FROM account_dataset_coverage
        WHERE account_id=:account_id AND dataset_id=:dataset_id"""), {
        "account_id": account_id, "dataset_id": dataset_id,
    }).scalar_one_or_none() or 0
    return {"status": state, "dataset_id": str(dataset_id), "pending_sources": pending,
            "failed_sources": failed, "visited_node_count": int(visited) if state == "ready" else None,
            "unsupported_sample_count": int(counts["unsupported"]) if state == "ready" else None,
            "progress_revision": int(version)}


def get_account_coverage(db: Connection, account_id: str, dataset_id: int) -> dict[str, Any]:
    """Return live readiness and counts; cached summary flags never override source/job truth."""
    return _live_summary(db, account_id, dataset_id)


def _rebuild_account_coverage(db: Connection, account_id: str, dataset_id: int) -> dict[str, Any]:
    db.execute(text("""INSERT INTO account_dataset_coverage(account_id,dataset_id)
        VALUES (:account_id,:dataset_id) ON CONFLICT DO NOTHING"""), {
        "account_id": account_id, "dataset_id": dataset_id,
    })
    db.execute(text("""SELECT account_id FROM account_dataset_coverage
        WHERE account_id=:account_id AND dataset_id=:dataset_id FOR UPDATE"""), {
        "account_id": account_id, "dataset_id": dataset_id,
    }).first()
    live = _live_summary(db, account_id, dataset_id)
    db.execute(text("""UPDATE account_dataset_coverage SET status=:status,
            pending_sources=:pending, failed_sources=:failed, visited_node_count=:visited,
            unsupported_sample_count=:unsupported, progress_revision=progress_revision+1,
            updated_at=clock_timestamp()
        WHERE account_id=:account_id AND dataset_id=:dataset_id"""), {
        "status": live["status"], "pending": live["pending_sources"],
        "failed": live["failed_sources"], "visited": live["visited_node_count"] or 0,
        "unsupported": live["unsupported_sample_count"] or 0,
        "account_id": account_id, "dataset_id": dataset_id,
    })
    db.execute(text("DELETE FROM account_street_coverage WHERE account_id=:account_id AND dataset_id=:dataset_id"), {
        "account_id": account_id, "dataset_id": dataset_id,
    })
    db.execute(text("""INSERT INTO account_street_coverage
            (account_id,dataset_id,street_id,visited_node_count)
        SELECT CAST(:account_id AS varchar(255)), street.dataset_id, street.street_id, count(*)::integer
        FROM street_nodes street
        WHERE street.dataset_id=:dataset_id AND EXISTS (
            SELECT 1 FROM source_node_contributions contribution
            JOIN activity_sources source ON source.account_id=contribution.account_id
                AND source.id=contribution.source_id AND source.revision=contribution.source_revision
            WHERE contribution.account_id=:account_id AND contribution.dataset_id=street.dataset_id
              AND contribution.node_id=street.osm_node_id
              AND source.status='succeeded' AND source.activity_id IS NOT NULL
        )
        GROUP BY street.dataset_id, street.street_id"""), {
        "account_id": account_id, "dataset_id": dataset_id,
    })
    return live


def rebuild_account_coverage(bind: Engine | Connection, account_id: str, dataset_id: int) -> dict[str, Any]:
    """Rebuild account/dataset and per-street summaries from current source support."""
    if isinstance(bind, Engine):
        with bind.begin() as db:
            return _rebuild_account_coverage(db, account_id, dataset_id)
    return _rebuild_account_coverage(bind, account_id, dataset_id)


def get_street_coverage(db: Connection, account_id: str, dataset_id: int,
                        street_id: int, rule: str = "normal") -> dict[str, Any]:
    if rule not in {"normal", "strict"}:
        raise ValueError("rule must be 'normal' or 'strict'")
    version = get_account_coverage(db, account_id, dataset_id)
    row = db.execute(text("""SELECT eligible_node_count FROM streets
        WHERE dataset_id=:dataset_id AND id=:street_id"""), {
        "dataset_id": dataset_id, "street_id": street_id,
    }).first()
    if row is None:
        return {"status": "not_found", "dataset_id": str(dataset_id), "street_id": str(street_id),
                "rule": rule, "visited": None, "total": None, "threshold": None, "state": None}
    total = int(row[0])
    if version["status"] != "ready":
        return {"status": version["status"], "dataset_id": str(dataset_id), "street_id": str(street_id),
                "rule": rule, "visited": None, "total": total, "threshold": None, "state": None}
    visited = int(db.execute(text("""SELECT count(*) FROM street_nodes street
        WHERE street.dataset_id=:dataset_id AND street.street_id=:street_id
          AND EXISTS (
              SELECT 1 FROM source_node_contributions contribution
              JOIN activity_sources source ON source.account_id=contribution.account_id
                  AND source.id=contribution.source_id AND source.revision=contribution.source_revision
              WHERE contribution.account_id=:account_id AND contribution.dataset_id=street.dataset_id
                AND contribution.node_id=street.osm_node_id
                AND source.status='succeeded' AND source.activity_id IS NOT NULL
          )"""), {"dataset_id": dataset_id, "street_id": street_id,
                   "account_id": account_id}).scalar_one())
    threshold = total if rule == "strict" or total < 10 else (9 * total + 9) // 10
    state = "complete" if total > 0 and visited >= threshold else "partial" if visited else "missing"
    return {"status": "ready", "dataset_id": str(dataset_id), "street_id": str(street_id),
            "rule": rule, "visited": visited, "total": total, "threshold": threshold, "state": state}


def _locked_context(db: Connection, job_id: int, lease_token: str,
                    source_id: int, source_revision: int, dataset_id: int):
    job = db.execute(text("""SELECT id,account_id,kind,payload FROM jobs
        WHERE id=:id AND status='running' AND lease_token=:token
          AND leased_until > clock_timestamp() FOR UPDATE"""), {
        "id": job_id, "token": lease_token,
    }).mappings().first()
    if job is None or job["kind"] != "match_coverage":
        return None, None, None
    source = db.execute(text("""SELECT id,account_id,revision,status,activity_id
        FROM activity_sources WHERE id=:id FOR UPDATE"""), {"id": source_id}).mappings().first()
    if (source is None or source["account_id"] != job["account_id"]
            or source["revision"] != source_revision or source["status"] != "succeeded"
            or source["activity_id"] is None):
        return job, source, None
    run = db.execute(text("""SELECT * FROM coverage_source_runs
        WHERE account_id=:account_id AND source_id=:source_id AND source_revision=:source_revision
          AND dataset_id=:dataset_id FOR UPDATE"""), {
        "account_id": source["account_id"], "source_id": source_id,
        "source_revision": source_revision, "dataset_id": dataset_id,
    }).mappings().first()
    if run is None or run["job_id"] != job_id:
        return job, source, None
    return job, source, run


def _job_payload(job: dict[str, Any]) -> tuple[int, int, int] | None:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return None
    source_id, source_revision, dataset_id = (payload.get("source_id"), payload.get("source_revision"),
                                               payload.get("dataset_id"))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1
           for value in (source_id, source_revision, dataset_id)):
        return None
    return source_id, source_revision, dataset_id


def _active_dataset_count(db: Connection) -> int:
    return int(db.execute(text("""SELECT count(*) FROM map_datasets
        WHERE status='active' AND coverage_mode='complete'""")).scalar_one())


def _refresh_activity_processing(db: Connection, *, account_id: str, source_id: int,
                                 source_revision: int, activity_id: int) -> None:
    # Acquire the activity lock before reading run/job truth so a concurrent
    # matcher cannot commit success while this refresh still writes stale false.
    tracks = db.execute(text("SELECT tracks FROM activities WHERE id=:id AND user_id=:account_id FOR UPDATE"), {
        "id": activity_id, "account_id": account_id,
    }).scalar_one_or_none()
    if tracks is None:
        return
    points = _samples_from_tracks(tracks)
    active_count = _active_dataset_count(db)
    unmapped = _count_unsupported_samples(db, points)
    pending = db.execute(text("""SELECT count(*) FROM map_datasets dataset
        WHERE dataset.status='active' AND dataset.coverage_mode='complete'
          AND NOT EXISTS (
              SELECT 1 FROM coverage_source_runs run JOIN jobs job
                ON job.account_id=run.account_id AND job.id=run.job_id
              WHERE run.account_id=:account_id AND run.source_id=:source_id
                AND run.source_revision=:source_revision AND run.dataset_id=dataset.id
                AND run.status='succeeded' AND job.status='succeeded'
          )"""), {"account_id": account_id, "source_id": source_id,
                   "source_revision": source_revision}).scalar_one()
    db.execute(text("""UPDATE activities SET processed=:processed, unmapped_points=:unmapped
        WHERE id=:activity_id AND user_id=:account_id"""), {
        "processed": active_count > 0 and int(pending) == 0,
        "unmapped": unmapped if active_count else len(points),
        "activity_id": activity_id, "account_id": account_id,
    })


def _record_failure(engine: Engine, job: dict[str, Any], payload: tuple[int, int, int], code: str) -> None:
    source_id, source_revision, dataset_id = payload
    with engine.begin() as db:
        owner_lock(db, str(job.get("account_id", "")))
        locked_job, source, run = _locked_context(db, int(job["id"]), str(job["lease_token"]),
                                                  source_id, source_revision, dataset_id)
        if locked_job is None:
            return
        if source is None or run is None:
            fail(db, job_id=int(job["id"]), lease_token=str(job["lease_token"]),
                 error_code="stale_coverage_job", permanent=True)
            return
        if not fail(db, job_id=int(job["id"]), lease_token=str(job["lease_token"]), error_code=code):
            return
        job_status = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": job["id"]}).scalar_one()
        db.execute(text("""UPDATE coverage_source_runs SET status=:status,last_error=:error,
                updated_at=clock_timestamp()
            WHERE account_id=:account_id AND source_id=:source_id
              AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
            "status": "failed" if job_status == "failed" else "queued", "error": code,
            "account_id": source["account_id"], "source_id": source_id,
            "source_revision": source_revision, "dataset_id": dataset_id,
        })
        _rebuild_account_coverage(db, source["account_id"], dataset_id)


def process_source_dataset(engine: Engine, job: dict[str, Any], *,
                           batch_size: int = DEFAULT_BATCH_SIZE) -> None:
    """Match original GPS samples against all nearby indexed nodes; never interpolate or cap results."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    payload = _job_payload(job)
    if payload is None:
        fail(engine, job_id=int(job["id"]), lease_token=str(job["lease_token"]),
             error_code="invalid_coverage_payload", permanent=True)
        return
    source_id, source_revision, dataset_id = payload
    job_id = int(job["id"])
    lease_token = str(job["lease_token"])
    try:
        with engine.begin() as db:
            owner_lock(db, str(job.get("account_id", "")))
            locked_job, source, run = _locked_context(db, job_id, lease_token,
                                                      source_id, source_revision, dataset_id)
            if locked_job is None:
                return
            if source is None or run is None:
                fail(db, job_id=job_id, lease_token=lease_token,
                     error_code="stale_coverage_job", permanent=True)
                return
            if run["status"] == "succeeded":
                complete(db, job_id=job_id, lease_token=lease_token)
                return
            dataset = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id FOR SHARE"), {
                "id": dataset_id,
            }).mappings().first()
            if dataset is None or dataset["coverage_mode"] != "complete" or dataset["status"] == "retired":
                fail(db, job_id=job_id, lease_token=lease_token,
                     error_code="unsupported_coverage_dataset", permanent=True)
                db.execute(text("""UPDATE coverage_source_runs SET status='failed',last_error='unsupported_coverage_dataset',
                    updated_at=clock_timestamp() WHERE account_id=:account_id AND source_id=:source_id
                      AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
                    "account_id": source["account_id"], "source_id": source_id,
                    "source_revision": source_revision, "dataset_id": dataset_id,
                })
                _rebuild_account_coverage(db, source["account_id"], dataset_id)
                return
            activity = db.execute(text("SELECT tracks FROM activities WHERE id=:id AND user_id=:account_id FOR SHARE"), {
                "id": source["activity_id"], "account_id": source["account_id"],
            }).scalar_one_or_none()
            points = _samples_from_tracks(activity)
            db.execute(text("""INSERT INTO account_dataset_coverage(account_id,dataset_id)
                VALUES (:account_id,:dataset_id) ON CONFLICT DO NOTHING"""), {
                "account_id": source["account_id"], "dataset_id": dataset_id,
            })
            db.execute(text("""SELECT account_id FROM account_dataset_coverage
                WHERE account_id=:account_id AND dataset_id=:dataset_id FOR UPDATE"""), {
                "account_id": source["account_id"], "dataset_id": dataset_id,
            }).first()
            db.execute(text("""UPDATE coverage_source_runs SET status='running',sample_count=:count,
                supported_sample_count=0,unsupported_sample_count=0,matched_node_count=0,
                last_error=NULL,updated_at=clock_timestamp()
                WHERE account_id=:account_id AND source_id=:source_id
                  AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
                "count": len(points), "account_id": source["account_id"], "source_id": source_id,
                "source_revision": source_revision, "dataset_id": dataset_id,
            })
            supported = 0
            for start in range(0, len(points), batch_size):
                batch = points[start:start + batch_size]
                renewed = db.execute(text("""UPDATE jobs SET leased_until=GREATEST(leased_until,
                        clock_timestamp()+interval '60 seconds'),
                        updated_at=clock_timestamp()
                    WHERE id=:id AND status='running' AND lease_token=:token
                      AND leased_until > clock_timestamp()"""), {"id": job_id, "token": lease_token})
                if renewed.rowcount != 1:
                    raise RuntimeError("coverage_lease_expired")
                supported += _count_dataset_supported_samples(db, dataset_id, batch)
                _insert_batch(db, account_id=source["account_id"], source_id=source_id,
                              source_revision=source_revision, dataset_id=dataset_id, points=batch)
            matched = db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account_id AND source_id=:source_id
                  AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
                "account_id": source["account_id"], "source_id": source_id,
                "source_revision": source_revision, "dataset_id": dataset_id,
            }).scalar_one()
            db.execute(text("""UPDATE coverage_source_runs SET status='succeeded',
                    supported_sample_count=:supported,unsupported_sample_count=:unsupported,
                    matched_node_count=:matched,last_error=NULL,updated_at=clock_timestamp()
                WHERE account_id=:account_id AND source_id=:source_id
                  AND source_revision=:source_revision AND dataset_id=:dataset_id"""), {
                "supported": supported, "unsupported": len(points) - supported,
                "matched": matched, "account_id": source["account_id"], "source_id": source_id,
                "source_revision": source_revision, "dataset_id": dataset_id,
            })
            if not complete(db, job_id=job_id, lease_token=lease_token):
                raise RuntimeError("coverage_lease_expired")
            _refresh_activity_processing(db, account_id=source["account_id"], source_id=source_id,
                                         source_revision=source_revision, activity_id=source["activity_id"])
            _rebuild_account_coverage(db, source["account_id"], dataset_id)
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and _ERROR_CODE.fullmatch(str(exc)) else type(exc).__name__.lower()
        code = f"coverage_{code}"[:64]
        if not _ERROR_CODE.fullmatch(code):
            code = "coverage_handler_error"
        _record_failure(engine, job, payload, code)
        raise


def remove_source_support(db: Connection, *, account_id: str, source_id: int,
                          source_revision: int) -> list[int]:
    """Remove only one source revision's node support and rebuild affected account summaries."""
    datasets = db.execute(text("""SELECT dataset_id FROM coverage_source_runs
        WHERE account_id=:account_id AND source_id=:source_id AND source_revision=:revision
        UNION SELECT dataset_id FROM source_node_contributions
        WHERE account_id=:account_id AND source_id=:source_id AND source_revision=:revision"""), {
        "account_id": account_id, "source_id": source_id, "revision": source_revision,
    }).scalars().all()
    db.execute(text("""DELETE FROM source_node_contributions
        WHERE account_id=:account_id AND source_id=:source_id AND source_revision=:revision"""), {
        "account_id": account_id, "source_id": source_id, "revision": source_revision,
    })
    db.execute(text("""DELETE FROM coverage_source_runs
        WHERE account_id=:account_id AND source_id=:source_id AND source_revision=:revision"""), {
        "account_id": account_id, "source_id": source_id, "revision": source_revision,
    })
    for dataset_id in datasets:
        _rebuild_account_coverage(db, account_id, dataset_id)
    return list(datasets)


def requeue_dataset_coverage(engine: Engine, dataset_id: int, *, batch_size: int = 100,
                             max_sources: int = 1000, after_source_id: int = 0) -> dict[str, Any]:
    """Queue a bounded keyset page of existing successfully ingested sources for one dataset."""
    if not 1 <= batch_size <= 500 or not 1 <= max_sources <= MAX_REQUEUE_SOURCES or after_source_id < 0:
        raise ValueError("invalid coverage requeue bounds")
    total = 0
    cursor = after_source_id
    while total < max_sources:
        size = min(batch_size, max_sources - total)
        with engine.begin() as db:
            dataset = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id"), {
                "id": dataset_id,
            }).mappings().first()
            if dataset is None or dataset["status"] == "retired" or dataset["coverage_mode"] != "complete":
                raise ValueError("coverage_dataset_unavailable")
            sources = db.execute(text("""SELECT source.id,source.account_id,source.revision,source.activity_id
                FROM activity_sources source JOIN activities activity
                  ON activity.user_id=source.account_id AND activity.id=source.activity_id
                WHERE source.status='succeeded' AND source.activity_id IS NOT NULL AND source.id>:after_id
                ORDER BY source.id LIMIT :limit"""), {"after_id": cursor, "limit": size}).mappings().all()
            if not sources:
                break
            for source in sources:
                owner_lock(db, source["account_id"])
                current = db.execute(text("""SELECT revision,activity_id,status FROM activity_sources
                    WHERE id=:id AND account_id=:account"""), {
                    "id": source["id"], "account": source["account_id"],
                }).mappings().first()
                if current is None or current["status"] != "succeeded" or current["revision"] != source["revision"]:
                    continue
                queue_source_coverage(db, account_id=source["account_id"], source_id=source["id"],
                                      source_revision=source["revision"], activity_id=source["activity_id"],
                                      dataset_ids=[dataset_id], priority=10)
                cursor = source["id"]
                total += 1
        if len(sources) < size:
            break
    with engine.connect() as db:
        more = db.execute(text("""SELECT EXISTS(SELECT 1 FROM activity_sources
            WHERE status='succeeded' AND activity_id IS NOT NULL AND id>:after_id)"""), {
            "after_id": cursor,
        }).scalar_one()
    return {"dataset_id": str(dataset_id), "sources_queued": total,
            "next_after_source_id": cursor, "has_more": bool(more)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Requeue a bounded page of source coverage jobs")
    parser.add_argument("--dataset-id", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-sources", type=int, default=1000)
    parser.add_argument("--after-source-id", type=int, default=0)
    args = parser.parse_args()
    engine = create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg://localhost/activities"),
                           pool_pre_ping=True)
    try:
        result = requeue_dataset_coverage(engine, args.dataset_id, batch_size=args.batch_size,
                                          max_sources=args.max_sources, after_source_id=args.after_source_id)
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
