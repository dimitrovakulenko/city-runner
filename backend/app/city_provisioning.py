"""Owner-scoped status and durable preparation work for automatic city discovery."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import uuid
from bisect import bisect_left
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from backend.app.owner_guard import owner_lock

MAX_OWNER_CITIES = 20
MAX_OWNER_ENROLLMENT = 20
MAX_PROVIDER_ATTEMPTS = 5
PROVIDER_LEASE_SECONDS = 300
PUBLIC_TASK_LIMIT = 100
PUBLIC_DAILY_BYTES = 268_435_456
PUBLIC_REQUEST_GAP_SECONDS = 2
MAX_OWNER_COUNTRIES = 20
MAX_CITY_CANDIDATES_PER_STEP = 20


class CoveragePreparationCity(BaseModel):
    osm_relation_id: str
    name: str
    state: Literal["queued", "fetching", "importing", "ready", "failed"]
    error: str | None


class CoveragePreparationResponse(BaseModel):
    enabled: bool
    state: Literal["ready", "pending", "failed", "unsupported"]
    pending_sources: int
    failed_sources: int
    unsupported_sources: int
    cities: list[CoveragePreparationCity]
    cities_truncated: bool


def enabled() -> bool:
    return os.getenv("CITY_GEOGRAPHY_PROVIDER", "").strip().lower() == "overpass"


def ensure_public_task(db: Connection, task_key: str, task_kind: str, *, country_code: str | None = None,
                       admin_levels: list[int] | None = None, relation_id: int | None = None,
                       city_name: str | None = None) -> None:
    db.execute(text("""INSERT INTO city_public_tasks
        (task_key,task_kind,country_code,admin_levels,relation_id,city_name)
        VALUES (:key,:kind,:country,CAST(:levels AS jsonb),:relation,:name)
        ON CONFLICT (task_key) DO NOTHING"""), {
        "key": task_key, "kind": task_kind, "country": country_code,
        "levels": json.dumps(admin_levels) if admin_levels is not None else None,
        "relation": relation_id, "name": city_name,
    })


def _provider_versions() -> tuple[str, str]:
    from backend.app.city_geography_provider import PROFILE_VERSION, QUERY_VERSION
    return PROFILE_VERSION, QUERY_VERSION


def _city_task_key(relation_id: int) -> str:
    from backend.app.geography import ELIGIBILITY_RULE_VERSION
    profile_version, query_version = _provider_versions()
    return f"city:{relation_id}:{profile_version}:{query_version}:{ELIGIBILITY_RULE_VERSION}"


def enqueue_preparation_request(db: Connection, *, account_id: str, source_id: int,
                                source_revision: int) -> None:
    """Call from the successful source-ingestion transaction; deliberately stores no GPS."""
    if not enabled():
        return
    ensure_public_task(db, "world:" + _provider_versions()[0], "world")
    db.execute(text("""INSERT INTO activity_preparation_requests
        (account_id,source_id,source_revision,state,phase)
        SELECT CAST(:account AS varchar),CAST(:source AS bigint),CAST(:revision AS integer),'pending','catalog'
        WHERE EXISTS (SELECT 1 FROM activity_sources WHERE account_id=CAST(:account AS varchar)
            AND id=CAST(:source AS bigint) AND revision=CAST(:revision AS integer)
            AND status='succeeded' AND activity_id IS NOT NULL)
        ON CONFLICT (account_id,source_id,source_revision) DO NOTHING"""), {
        "account": account_id, "source": source_id, "revision": source_revision,
    })


def get_coverage_preparation(db: Connection, account_id: str) -> dict[str, Any]:
    owner_lock(db, account_id)
    if not enabled():
        return {"enabled": False, "state": "ready", "pending_sources": 0,
                "failed_sources": 0, "unsupported_sources": 0, "cities": [], "cities_truncated": False}
    counts = db.execute(text("""SELECT
            count(*) FILTER (WHERE request.source_id IS NULL OR request.state='pending') AS pending,
            count(*) FILTER (WHERE request.state='failed') AS failed,
            count(*) FILTER (WHERE request.state='unsupported') AS unsupported
        FROM activity_sources source
        LEFT JOIN activity_preparation_requests request ON request.account_id=source.account_id
          AND request.source_id=source.id AND request.source_revision=source.revision
        WHERE source.account_id=:account AND source.status='succeeded' AND source.activity_id IS NOT NULL"""), {
        "account": account_id,
    }).mappings().one()
    failed, pending, unsupported = (int(counts[name]) for name in ("failed", "pending", "unsupported"))
    state = "failed" if failed else "pending" if pending else "unsupported" if unsupported else "ready"
    rows = db.execute(text("""SELECT relation_id,name,state,error FROM (
        SELECT DISTINCT ON (city.relation_id) city.relation_id,city.name,
            CASE WHEN city.state='ready' AND dataset.status='active' THEN 'ready'
                 WHEN request.state='failed' OR city.state='failed' OR task.status='failed' THEN 'failed'
                 WHEN city.dataset_id IS NOT NULL OR task.map_import_task_id IS NOT NULL THEN 'importing'
                 WHEN task.status='running' THEN 'fetching' ELSE 'queued' END AS state,
            COALESCE(task.safe_error,request.safe_error) AS error
        FROM activity_preparation_cities city
        JOIN activity_preparation_requests request USING(account_id,source_id,source_revision)
        JOIN activity_sources source ON source.account_id=request.account_id
          AND source.id=request.source_id AND source.revision=request.source_revision
        JOIN city_public_tasks task ON task.task_key=city.task_key
        LEFT JOIN map_datasets dataset ON dataset.id=city.dataset_id
        WHERE city.account_id=:account AND source.status='succeeded' AND source.activity_id IS NOT NULL
        ORDER BY city.relation_id,city.name
        ) cities ORDER BY name,relation_id LIMIT :limit"""), {
        "account": account_id, "limit": MAX_OWNER_CITIES + 1,
    }).mappings().all()
    cities = [{"osm_relation_id": str(row["relation_id"]), "name": row["name"],
               "state": row["state"], "error": row["error"]} for row in rows[:MAX_OWNER_CITIES]]
    return {"enabled": True, "state": state, "pending_sources": pending,
            "failed_sources": failed, "unsupported_sources": unsupported, "cities": cities,
            "cities_truncated": len(rows) > MAX_OWNER_CITIES}


def retry_coverage_preparation(db: Connection, account_id: str) -> None:
    owner_lock(db, account_id)
    if not enabled():
        return
    profile_version, _ = _provider_versions()
    db.execute(text("""UPDATE city_public_tasks SET status='queued',attempts=0,
        available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,safe_error=NULL,
        updated_at=clock_timestamp()
        WHERE task_key=:key AND status='failed'
          AND EXISTS (SELECT 1 FROM activity_preparation_requests request
              WHERE request.account_id=:account AND request.state='failed')"""), {
        "key": f"world:{profile_version}", "account": account_id,
    })
    request_rows = db.execute(text("""SELECT request.source_id,request.source_revision
        FROM activity_preparation_requests request JOIN activity_sources source
          ON source.account_id=request.account_id AND source.id=request.source_id
            AND source.revision=request.source_revision
        WHERE request.account_id=:account AND request.state='failed' AND source.status='succeeded'
          AND source.activity_id IS NOT NULL
        ORDER BY request.updated_at,request.source_id LIMIT :limit FOR UPDATE OF request"""), {
        "account": account_id, "limit": MAX_OWNER_ENROLLMENT,
    }).mappings().all()
    for row in request_rows:
        db.execute(text("""UPDATE activity_preparation_requests SET state='pending',phase='catalog',
            safe_error=NULL,updated_at=clock_timestamp()
            WHERE account_id=:account AND source_id=:source AND source_revision=:revision"""), {
            "account": account_id, "source": row["source_id"], "revision": row["source_revision"],
        })
        db.execute(text("""UPDATE city_public_tasks task SET status='queued',attempts=0,
            available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,safe_error=NULL,
            updated_at=clock_timestamp()
            WHERE task.task_key IN (SELECT task_key FROM (
                SELECT task_key FROM activity_preparation_countries WHERE account_id=:account
                  AND source_id=:source AND source_revision=:revision
                UNION SELECT task_key FROM activity_preparation_cities WHERE account_id=:account
                  AND source_id=:source AND source_revision=:revision
                ) linked ORDER BY task_key LIMIT :task_limit)
              AND task.status='failed'"""), {
            "account": account_id, "source": row["source_id"], "revision": row["source_revision"],
            "task_limit": MAX_OWNER_ENROLLMENT,
        })
        db.execute(text("""UPDATE map_import_tasks task SET
            status=CASE WHEN phase='backfill' THEN 'backfilling' ELSE 'queued' END,attempts=0,
            available_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,last_error=NULL,
            updated_at=clock_timestamp()
            WHERE task.id IN (SELECT city_task.map_import_task_id FROM activity_preparation_cities city
                JOIN city_public_tasks city_task ON city_task.task_key=city.task_key
                WHERE city.account_id=:account AND city.source_id=:source
                  AND city.source_revision=:revision
                ORDER BY city_task.map_import_task_id LIMIT :task_limit)
              AND task.task_type='osm_import' AND task.status='failed'"""), {
            "account": account_id, "source": row["source_id"], "revision": row["source_revision"],
            "task_limit": MAX_OWNER_ENROLLMENT,
        })


def _points(tracks: Any) -> list[list[float]]:
    from backend.app.coverage import _samples_from_tracks
    return _samples_from_tracks(tracks)


def _world_candidate_countries(db: Connection, regions: Any, points: list[list[float]]) -> list[str]:
    candidates = []
    if not isinstance(regions, list):
        raise ValueError("invalid_city_provider_response")
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("country_code"), str):
            raise ValueError("invalid_city_provider_response")
        geometry = region.get("geometry")
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        positions = []

        def visit(value):
            if isinstance(value, list) and len(value) == 2 and all(type(part) in (int, float) for part in value):
                positions.append(value)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(coordinates)
        if not positions:
            continue
        west = min(position[0] for position in positions)
        east = max(position[0] for position in positions)
        south = min(position[1] for position in positions)
        north = max(position[1] for position in positions)
        if any(west <= lon <= east and south <= lat <= north for lon, lat in points):
            candidates.append(region)
    if not candidates or not points:
        return []
    from backend.app.coverage import _points_json

    valid = db.execute(text("""SELECT bool_and(ST_IsValid(
            ST_SetSRID(ST_GeomFromGeoJSON((value->'geometry')::text),4326)))
        FROM jsonb_array_elements(CAST(:regions AS jsonb)) AS item(value)"""), {
        "regions": json.dumps(candidates, allow_nan=False, separators=(",", ":")),
    }).scalar_one()
    if not valid:
        raise ValueError("invalid_city_provider_response")

    result = db.execute(text("""WITH regions AS (
            SELECT value->>'country_code' AS country_code,
                ST_SetSRID(ST_GeomFromGeoJSON((value->'geometry')::text),4326) AS boundary
            FROM jsonb_array_elements(CAST(:regions AS jsonb)) AS item(value)
        ), samples AS (
            SELECT ST_SetSRID(ST_MakePoint((point->>0)::double precision,
                (point->>1)::double precision),4326) AS position
            FROM jsonb_array_elements(CAST(:points AS jsonb)) AS item(point)
        )
        SELECT DISTINCT region.country_code
        FROM regions region JOIN samples sample
          ON region.boundary && sample.position AND ST_Covers(region.boundary,sample.position)
        ORDER BY region.country_code"""), {
        "regions": json.dumps(candidates, allow_nan=False, separators=(",", ":")),
        "points": _points_json(points),
    }).scalars().all()
    return [str(country) for country in result]


def _request_points(db: Connection, account_id: str, source_id: int, revision: int) -> list[list[float]] | None:
    row = db.execute(text("""SELECT activity.tracks FROM activity_preparation_requests request
        JOIN activity_sources source ON source.account_id=request.account_id AND source.id=request.source_id
          AND source.revision=request.source_revision
        JOIN activities activity ON activity.user_id=source.account_id AND activity.id=source.activity_id
        WHERE request.account_id=:account AND request.source_id=:source AND request.source_revision=:revision
          AND source.status='succeeded' AND source.activity_id IS NOT NULL"""), {
        "account": account_id, "source": source_id, "revision": revision,
    }).scalar_one_or_none()
    return _points(row) if row is not None else None


def _active_relation_dataset(db: Connection, relation_id: int) -> int | None:
    from backend.app.geography import ELIGIBILITY_RULE_VERSION
    return db.execute(text("""SELECT dataset.id FROM map_datasets dataset JOIN cities city
        ON city.dataset_id=dataset.id WHERE dataset.status='active' AND dataset.coverage_mode='complete'
          AND dataset.eligibility_rule_version=:rule
          AND city.osm_relation_id=:relation ORDER BY dataset.id DESC LIMIT 1"""), {
        "relation": relation_id, "rule": ELIGIBILITY_RULE_VERSION,
    }).scalar_one_or_none()


def _mark_preparation_failed(db: Connection, account_id: str, source_id: int,
                             revision: int, error: str) -> None:
    db.execute(text("""UPDATE activity_preparation_requests SET state='failed',phase='done',
        safe_error=:error,updated_at=clock_timestamp()
        WHERE account_id=:account AND source_id=:source AND source_revision=:revision"""), {
        "account": account_id, "source": source_id, "revision": revision, "error": error,
    })


def _defer_preparation(db: Connection, account_id: str, source_id: int, revision: int) -> None:
    db.execute(text("""UPDATE activity_preparation_requests SET updated_at=clock_timestamp()
        WHERE account_id=:account AND source_id=:source AND source_revision=:revision
          AND state='pending'"""), {
        "account": account_id, "source": source_id, "revision": revision,
    })


def _enqueue_country_page(db: Connection, account: str, source_id: int, revision: int,
                          countries: list[str], cursor: str | None) -> str | None:
    from backend.app.city_geography_provider import country_admin_levels

    profile_version, query_version = _provider_versions()
    page = [code for code in countries if cursor is None or code > cursor][:MAX_OWNER_COUNTRIES]
    if not page:
        return cursor
    policies = country_admin_levels()
    for country in page:
        levels = policies.get(country)
        if levels is None:
            continue
        key = f"country:{country}:{profile_version}:{','.join(map(str, levels))}:{query_version}"
        ensure_public_task(db, key, "country", country_code=country, admin_levels=levels)
        db.execute(text("""INSERT INTO activity_preparation_countries
            (account_id,source_id,source_revision,country_code,task_key)
            VALUES (:account,:source,:revision,:country,:key) ON CONFLICT DO NOTHING"""), {
            "account": account, "source": source_id, "revision": revision,
            "country": country, "key": key,
        })
    return page[-1]


def _prepare_one_owner_step(engine: Engine) -> bool:
    with engine.connect() as db:
        candidate = db.execute(text("""SELECT account_id,source_id,source_revision
            FROM activity_preparation_requests WHERE state='pending'
            ORDER BY updated_at,account_id,source_id LIMIT 1""")).mappings().first()
    if candidate is None:
        return False
    account, source_id, revision = str(candidate["account_id"]), int(candidate["source_id"]), int(candidate["source_revision"])
    with engine.begin() as db:
        try:
            owner_lock(db, account)
        except HTTPException as error:
            if error.status_code in {401, 410}:
                return True
            raise
        request = db.execute(text("""SELECT * FROM activity_preparation_requests
            WHERE account_id=:account AND source_id=:source AND source_revision=:revision
              AND state='pending' FOR UPDATE"""), {
            "account": account, "source": source_id, "revision": revision,
        }).mappings().first()
        if request is None:
            return True
        points = _request_points(db, account, source_id, revision)
        if points is None:
            db.execute(text("""DELETE FROM activity_preparation_requests WHERE account_id=:account
                AND source_id=:source AND source_revision=:revision"""), {
                "account": account, "source": source_id, "revision": revision,
            })
            return True
        profile_version, query_version = _provider_versions()
        if request["phase"] == "catalog":
            world = db.execute(text("""SELECT status,result_json,safe_error FROM city_public_tasks
                WHERE task_key=:key"""), {"key": f"world:{profile_version}"}).mappings().one_or_none()
            if world is None or world["status"] not in {"succeeded", "failed"}:
                _defer_preparation(db, account, source_id, revision)
                return False
            if world["status"] == "failed":
                _mark_preparation_failed(db, account, source_id, revision, world["safe_error"] or "city_provider_unavailable")
                return True
            try:
                countries = _world_candidate_countries(db, world["result_json"].get("regions"), points)
            except (ValueError, TypeError, AttributeError):
                _mark_preparation_failed(db, account, source_id, revision, "invalid_city_provider_response")
                return True
            from backend.app.city_geography_provider import CityGeographyError
            db.execute(text("""UPDATE activity_preparation_requests SET country_codes=CAST(:countries AS jsonb),
                phase='country',updated_at=clock_timestamp()
                WHERE account_id=:account AND source_id=:source AND source_revision=:revision"""), {
                "countries": json.dumps(countries), "account": account, "source": source_id, "revision": revision,
            })
            try:
                cursor = _enqueue_country_page(db, account, source_id, revision, countries, None)
            except CityGeographyError:
                _mark_preparation_failed(db, account, source_id, revision, "invalid_city_country_policy")
                return True
            db.execute(text("""UPDATE activity_preparation_requests SET country_cursor=:cursor
                WHERE account_id=:account AND source_id=:source AND source_revision=:revision"""), {
                "cursor": cursor, "account": account, "source": source_id, "revision": revision,
            })
            if not countries:
                _finalize_preparation(db, account, source_id, revision, points)
            return True

        if request["phase"] == "country":
            countries = db.execute(text("""SELECT scope.country_code,scope.task_key,scope.state,
                    scope.candidate_cursor,
                    task.status AS task_status,task.result_json,task.safe_error
                FROM activity_preparation_countries scope JOIN city_public_tasks task USING(task_key)
                WHERE scope.account_id=:account AND scope.source_id=:source
                  AND scope.source_revision=:revision ORDER BY scope.country_code"""), {
                "account": account, "source": source_id, "revision": revision,
            }).mappings().all()
            if any(row["task_status"] == "failed" for row in countries):
                _mark_preparation_failed(db, account, source_id, revision, "city_provider_unavailable")
                return True
            if any(row["task_status"] != "succeeded" for row in countries):
                if countries:
                    _defer_preparation(db, account, source_id, revision)
                    return False
            pending_country = next((row for row in countries if row["state"] == "pending"), None)
            if pending_country is None:
                try:
                    cursor = _enqueue_country_page(db, account, source_id, revision,
                        list(request["country_codes"] or []), request["country_cursor"])
                except Exception:
                    _mark_preparation_failed(db, account, source_id, revision, "invalid_city_country_policy")
                    return True
                if cursor != request["country_cursor"]:
                    db.execute(text("""UPDATE activity_preparation_requests SET country_cursor=:cursor,
                        updated_at=clock_timestamp() WHERE account_id=:account AND source_id=:source
                          AND source_revision=:revision"""), {
                        "cursor": cursor, "account": account, "source": source_id, "revision": revision,
                    })
                    return True
                db.execute(text("""UPDATE activity_preparation_requests SET phase='city',
                    updated_at=clock_timestamp() WHERE account_id=:account AND source_id=:source
                      AND source_revision=:revision"""), {
                    "account": account, "source": source_id, "revision": revision,
                })
                return True
            candidates = pending_country["result_json"].get("cities")
            if not isinstance(candidates, list):
                _mark_preparation_failed(db, account, source_id, revision, "invalid_city_provider_response")
                return True
            cursor = int(pending_country["candidate_cursor"] or 0)
            ordered_points = sorted(points, key=lambda point: point[0])
            point_lons = [point[0] for point in ordered_points]
            page = candidates[cursor:cursor + 200]
            if not page:
                db.execute(text("""UPDATE activity_preparation_countries SET state='done'
                    WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                      AND country_code=:country"""), {
                    "account": account, "source": source_id, "revision": revision,
                    "country": pending_country["country_code"],
                })
                return True
            selected = 0
            for index, item in enumerate(page, start=cursor + 1):
                if not isinstance(item, dict) or type(item.get("relation_id")) is not int:
                    _mark_preparation_failed(db, account, source_id, revision, "invalid_city_provider_response")
                    return True
                relation = int(item["relation_id"])
                bounds = item.get("bounds")
                if (not isinstance(bounds, list) or len(bounds) != 4
                        or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                   for value in bounds)
                        or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
                    continue
                point_index = bisect_left(point_lons, bounds[0])
                if not any(point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]
                           for point in ordered_points[point_index:]):
                    continue
                task_key = _city_task_key(relation)
                ensure_public_task(db, task_key, "city", country_code=pending_country["country_code"],
                                   relation_id=relation, city_name=str(item["name"])[:200])
                dataset_id = _active_relation_dataset(db, relation)
                if dataset_id is not None:
                    db.execute(text("""UPDATE city_public_tasks SET status='succeeded'
                        WHERE task_key=:key AND status='queued'"""), {"key": task_key})
                db.execute(text("""INSERT INTO activity_preparation_cities
                    (account_id,source_id,source_revision,relation_id,name,task_key,dataset_id)
                    VALUES (:account,:source,:revision,:relation,:name,:key,:dataset)
                    ON CONFLICT DO NOTHING"""), {
                    "account": account, "source": source_id, "revision": revision,
                    "relation": relation, "name": str(item["name"])[:200],
                    "key": task_key, "dataset": dataset_id,
                })
                selected += 1
                if selected >= MAX_CITY_CANDIDATES_PER_STEP:
                    break
            next_cursor = cursor + (index - cursor if selected >= MAX_CITY_CANDIDATES_PER_STEP else len(page))
            db.execute(text("""UPDATE activity_preparation_countries SET candidate_cursor=:cursor
                WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                  AND country_code=:country"""), {
                "cursor": next_cursor, "account": account, "source": source_id, "revision": revision,
                "country": pending_country["country_code"],
            })
            if next_cursor >= len(candidates):
                db.execute(text("""UPDATE activity_preparation_countries SET state='done'
                    WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                      AND country_code=:country"""), {
                    "account": account, "source": source_id, "revision": revision,
                    "country": pending_country["country_code"],
                })
            return True

        if request["phase"] == "city":
            city = db.execute(text("""SELECT city.*,task.status AS task_status,task.safe_error,
                    task.map_import_task_id,map_task.status AS import_status,map_task.dataset_id AS imported_dataset
                FROM activity_preparation_cities city JOIN city_public_tasks task USING(task_key)
                LEFT JOIN map_import_tasks map_task ON map_task.id=task.map_import_task_id
                WHERE city.account_id=:account AND city.source_id=:source
                  AND city.source_revision=:revision AND city.state IN ('pending','importing')
                ORDER BY city.relation_id LIMIT 1 FOR UPDATE OF city"""), {
                "account": account, "source": source_id, "revision": revision,
            }).mappings().first()
            if city is None:
                _finalize_preparation(db, account, source_id, revision, points)
                return True
            if city["task_status"] == "failed" or city["import_status"] == "failed":
                _mark_preparation_failed(db, account, source_id, revision, city["safe_error"] or "city_import_failed")
                return True
            dataset_id = city["dataset_id"] or city["imported_dataset"]
            if dataset_id is None:
                _defer_preparation(db, account, source_id, revision)
                return False
            dataset = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id"), {
                "id": dataset_id,
            }).mappings().first()
            if dataset is None or dataset["status"] != "active" or dataset["coverage_mode"] != "complete":
                _defer_preparation(db, account, source_id, revision)
                return False
            from backend.app.coverage import _dataset_applicability, queue_source_coverage
            applicable, touched = _dataset_applicability(db, account_id=account, source_id=source_id,
                source_revision=revision, dataset_id=int(dataset_id), points=points)
            if not touched:
                db.execute(text("""DELETE FROM activity_preparation_cities
                    WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                      AND relation_id=:relation"""), {
                    "account": account, "source": source_id, "revision": revision,
                    "relation": city["relation_id"],
                })
            else:
                db.execute(text("""UPDATE activity_preparation_cities SET state='ready',dataset_id=:dataset,
                    touched=TRUE WHERE account_id=:account AND source_id=:source AND source_revision=:revision
                      AND relation_id=:relation"""), {
                    "dataset": dataset_id, "account": account, "source": source_id, "revision": revision,
                    "relation": city["relation_id"],
                })
                if applicable:
                    activity_id = db.execute(text("""SELECT activity_id FROM activity_sources
                        WHERE account_id=:account AND id=:source AND revision=:revision"""), {
                        "account": account, "source": source_id, "revision": revision,
                    }).scalar_one_or_none()
                    if activity_id is not None:
                        queue_source_coverage(db, account_id=account, source_id=source_id,
                            source_revision=revision, activity_id=int(activity_id), dataset_ids=[int(dataset_id)])
            return True

        return False


def _finalize_preparation(db: Connection, account: str, source_id: int, revision: int,
                          points: list[list[float]]) -> None:
    from backend.app.coverage import _count_unsupported_samples, _refresh_activity_processing
    unsupported = int(_count_unsupported_samples(db, points))
    state = "unsupported" if unsupported else "ready"
    db.execute(text("""UPDATE activity_preparation_requests SET state=:state,phase='done',
        unsupported_sample_count=:unsupported,safe_error=NULL,updated_at=clock_timestamp()
        WHERE account_id=:account AND source_id=:source AND source_revision=:revision"""), {
        "state": state, "unsupported": unsupported, "account": account,
        "source": source_id, "revision": revision,
    })
    activity_id = db.execute(text("""SELECT activity_id FROM activity_sources
        WHERE account_id=:account AND id=:source AND revision=:revision AND status='succeeded'"""), {
        "account": account, "source": source_id, "revision": revision,
    }).scalar_one_or_none()
    if activity_id is not None:
        _refresh_activity_processing(db, account_id=account, source_id=source_id,
                                     source_revision=revision, activity_id=int(activity_id))


def _enroll_existing_sources(engine: Engine) -> bool:
    if not enabled():
        return False
    with engine.connect() as db:
        candidates = db.execute(text("""SELECT source.account_id,source.id,source.revision
            FROM activity_sources source
            WHERE source.status='succeeded' AND source.activity_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM activity_preparation_requests request
                WHERE request.account_id=source.account_id AND request.source_id=source.id
                  AND request.source_revision=source.revision)
            ORDER BY source.account_id,source.id LIMIT :limit"""), {
            "limit": MAX_OWNER_ENROLLMENT,
        }).mappings().all()
    if not candidates:
        return False
    for row in candidates:
        account = str(row["account_id"])
        with engine.begin() as db:
            try:
                owner_lock(db, account)
            except HTTPException as error:
                if error.status_code in {401, 410}:
                    continue
                raise
            ensure_public_task(db, "world:" + _provider_versions()[0], "world")
            db.execute(text("""INSERT INTO activity_preparation_requests
                (account_id,source_id,source_revision,state,phase)
                SELECT CAST(:account AS varchar),CAST(:source AS bigint),CAST(:revision AS integer),
                    'pending','catalog'
                WHERE EXISTS (SELECT 1 FROM activity_sources WHERE account_id=CAST(:account AS varchar)
                  AND id=CAST(:source AS bigint) AND revision=CAST(:revision AS integer)
                  AND status='succeeded' AND activity_id IS NOT NULL)
                ON CONFLICT DO NOTHING"""), {
                "account": account, "source": row["id"], "revision": row["revision"],
            })
            db.execute(text("""UPDATE activities SET processed=FALSE
                WHERE user_id=:account AND id=(SELECT activity_id FROM activity_sources
                  WHERE account_id=:account AND id=:source AND revision=:revision
                    AND status='succeeded')"""), {
                "account": account, "source": row["id"], "revision": row["revision"],
            })
    return True


def _reconcile_disabled_requests(engine: Engine) -> bool:
    """Clear stale processed=false flags when optional public discovery is switched off."""
    from backend.app.coverage import track_may_touch_dataset_sql
    group_candidate_sql = track_may_touch_dataset_sql("group_activity.track_geometry", "dataset.id")
    with engine.connect() as db:
        rows = db.execute(text("""SELECT request.account_id,request.source_id,
                request.source_revision,source.activity_id
            FROM activity_preparation_requests request
            JOIN activity_sources source ON source.account_id=request.account_id
              AND source.id=request.source_id AND source.revision=request.source_revision
            JOIN activities activity ON activity.user_id=source.account_id AND activity.id=source.activity_id
            WHERE request.state IN ('pending','failed') AND source.status='succeeded'
              AND activity.processed=FALSE
              AND source.activity_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM map_datasets active_dataset
                  WHERE active_dataset.status='active' AND active_dataset.coverage_mode='complete')
              AND NOT EXISTS (SELECT 1 FROM activity_sources group_source
                JOIN activities group_activity ON group_activity.user_id=group_source.account_id
                    AND group_activity.id=group_source.activity_id
                WHERE group_source.account_id=source.account_id
                  AND group_source.activity_id=source.activity_id
                  AND (group_source.status<>'succeeded' OR EXISTS (
                    SELECT 1 FROM map_datasets dataset
                    LEFT JOIN source_dataset_applicability applicability
                      ON applicability.account_id=group_source.account_id
                        AND applicability.source_id=group_source.id
                        AND applicability.source_revision=group_source.revision
                        AND applicability.dataset_id=dataset.id
                    WHERE dataset.status='active' AND dataset.coverage_mode='complete'
                      AND (applicability.applicable IS TRUE OR
                           (applicability.applicable IS NULL AND {group_candidate_sql}))
                      AND NOT EXISTS (SELECT 1 FROM coverage_source_runs run JOIN jobs job
                        ON job.account_id=run.account_id AND job.id=run.job_id
                        WHERE run.account_id=group_source.account_id AND run.source_id=group_source.id
                          AND run.source_revision=group_source.revision AND run.dataset_id=dataset.id
                          AND run.status='succeeded' AND job.status='succeeded'
                          AND job.kind='match_coverage'
                          AND job.payload->>'source_id'=group_source.id::text
                          AND job.payload->>'source_revision'=group_source.revision::text
                          AND job.payload->>'dataset_id'=dataset.id::text))))
            ORDER BY request.account_id,request.source_id LIMIT :limit""".format(
                group_candidate_sql=group_candidate_sql)), {
            "limit": MAX_OWNER_ENROLLMENT,
        }).mappings().all()
    if not rows:
        return False
    from backend.app.coverage import _refresh_activity_processing
    for row in rows:
        with engine.begin() as db:
            account = str(row["account_id"])
            try:
                owner_lock(db, account)
            except HTTPException as error:
                if error.status_code in {401, 410}:
                    continue
                raise
            _refresh_activity_processing(db, account_id=account, source_id=int(row["source_id"]),
                source_revision=int(row["source_revision"]), activity_id=int(row["activity_id"]))
    return True


def _reserve_public_request(engine: Engine) -> dict[str, Any] | None:
    if not enabled():
        return None
    token = uuid.uuid4().hex
    with engine.begin() as db:
        admission = db.execute(text("SELECT * FROM city_provider_admission WHERE singleton=TRUE FOR UPDATE")).mappings().one()
        db.execute(text("""UPDATE city_provider_admission SET
            utc_day=(clock_timestamp() AT TIME ZONE 'UTC')::date,
            requests_reserved=CASE WHEN utc_day<>(clock_timestamp() AT TIME ZONE 'UTC')::date THEN 0 ELSE requests_reserved END,
            bytes_reserved=CASE WHEN utc_day<>(clock_timestamp() AT TIME ZONE 'UTC')::date THEN 0 ELSE bytes_reserved END,
            active_task_key=CASE WHEN active_lease_until<=clock_timestamp() THEN NULL ELSE active_task_key END,
            active_lease_until=CASE WHEN active_lease_until<=clock_timestamp() THEN NULL ELSE active_lease_until END,
            active_lease_token=CASE WHEN active_lease_until<=clock_timestamp() THEN NULL ELSE active_lease_token END
            WHERE singleton=TRUE"""))
        admission = db.execute(text("SELECT * FROM city_provider_admission WHERE singleton=TRUE")).mappings().one()
        if admission["active_task_key"] is not None or admission["next_request_at"] > db.execute(text("SELECT clock_timestamp()")).scalar_one():
            return None
        if admission["requests_reserved"] >= PUBLIC_TASK_LIMIT:
            return None
        from backend.app.city_geography_provider import MAX_CATALOG_BYTES
        db.execute(text("""WITH expired AS (
            SELECT task_key FROM city_public_tasks
            WHERE status='running' AND leased_until<=clock_timestamp() AND attempts>=5
            ORDER BY leased_until,task_key FOR UPDATE SKIP LOCKED LIMIT 100
        )
        UPDATE city_public_tasks task SET status='failed',lease_token=NULL,leased_until=NULL,
            safe_error='city_provider_unavailable',updated_at=clock_timestamp()
        FROM expired WHERE task.task_key=expired.task_key"""))
        rows = db.execute(text("""SELECT task_key,task_kind,country_code,admin_levels,relation_id,city_name,
                attempts,result_json FROM city_public_tasks
            WHERE (status='queued' AND available_at<=clock_timestamp() OR
                   status='running' AND leased_until<=clock_timestamp())
              AND attempts<5
            ORDER BY created_at,task_key FOR UPDATE SKIP LOCKED LIMIT 1""")).mappings().first()
        if rows is None:
            return None
        max_bytes = (64 * 1024 * 1024) if rows["task_kind"] == "city" else MAX_CATALOG_BYTES
        if int(admission["bytes_reserved"]) + max_bytes > PUBLIC_DAILY_BYTES:
            return None
        lease_until = db.execute(text("SELECT clock_timestamp()+interval '300 seconds'")).scalar_one()
        db.execute(text("""UPDATE city_provider_admission SET requests_reserved=requests_reserved+1,
            bytes_reserved=bytes_reserved+:bytes,next_request_at=clock_timestamp()+interval '2 seconds',
            active_task_key=:key,active_lease_until=:until,active_lease_token=:token WHERE singleton=TRUE"""), {
            "bytes": max_bytes, "key": rows["task_key"], "until": lease_until, "token": token,
        })
        db.execute(text("""UPDATE city_public_tasks SET status='running',attempts=attempts+1,
            lease_token=:token,leased_until=:until,updated_at=clock_timestamp()
            WHERE task_key=:key"""), {"token": token, "until": lease_until, "key": rows["task_key"]})
        return {**dict(rows), "lease_token": token, "max_bytes": max_bytes}


def _finish_public_request(engine: Engine, task: dict[str, Any], *, result: dict | None = None,
                           map_task_id: int | None = None, error_code: str | None = None,
                           source_checksum: str | None = None,
                           source_timestamp: str | None = None) -> None:
    key, token = task["task_key"], task["lease_token"]
    with engine.begin() as db:
        db.execute(text("SELECT singleton FROM city_provider_admission WHERE singleton=TRUE FOR UPDATE"))
        if error_code:
            updated = db.execute(text("""UPDATE city_public_tasks SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'queued' END,
                available_at=clock_timestamp()+make_interval(secs=>LEAST(300,power(2,attempts)::int)),
                lease_token=NULL,leased_until=NULL,safe_error=:error,updated_at=clock_timestamp()
                WHERE task_key=:key AND status='running' AND lease_token=:token
                  AND leased_until>clock_timestamp()"""), {"error": error_code, "key": key, "token": token})
        else:
            serialized = json.dumps(result, ensure_ascii=True, allow_nan=False,
                                    sort_keys=True, separators=(",", ":")) if result is not None else None
            checksum = source_checksum or (hashlib.sha256(serialized.encode("ascii")).hexdigest()
                                            if serialized is not None else None)
            updated = db.execute(text("""UPDATE city_public_tasks SET status='succeeded',result_json=CAST(:result AS jsonb),
                source_checksum=:checksum,source_timestamp=CAST(:timestamp AS timestamptz),
                map_import_task_id=:map_task,lease_token=NULL,leased_until=NULL,safe_error=NULL,
                updated_at=clock_timestamp()
                WHERE task_key=:key AND status='running' AND lease_token=:token
                  AND leased_until>clock_timestamp()"""), {
                "result": json.dumps(result, allow_nan=False) if result is not None else None,
                "checksum": checksum, "timestamp": source_timestamp or (
                    result.get("source_timestamp") if isinstance(result, dict) else None),
                "map_task": map_task_id, "key": key, "token": token,
            })
        if updated.rowcount != 1:
            raise RuntimeError("city_public_task_lease_lost")
        db.execute(text("""UPDATE city_provider_admission SET active_task_key=NULL,
            active_lease_until=NULL,active_lease_token=NULL
            WHERE singleton=TRUE AND active_task_key=:key AND active_lease_token=:token"""), {
            "key": key, "token": token,
        })


def _finish_city_import(db: Connection, task: dict[str, Any], map_task_id: int,
                        source_checksum: str, source_timestamp: str | None) -> None:
    """Fence public lease and link the import task in the same commit."""
    key, token = task["task_key"], task["lease_token"]
    db.execute(text("SELECT singleton FROM city_provider_admission WHERE singleton=TRUE FOR UPDATE"))
    updated = db.execute(text("""UPDATE city_public_tasks SET status='succeeded',
        source_checksum=:checksum,source_timestamp=CAST(:timestamp AS timestamptz),
        map_import_task_id=:map_task,lease_token=NULL,leased_until=NULL,safe_error=NULL,
        updated_at=clock_timestamp()
        WHERE task_key=:key AND status='running' AND lease_token=:token
          AND leased_until>clock_timestamp()"""), {
        "checksum": source_checksum, "timestamp": source_timestamp,
        "map_task": map_task_id, "key": key, "token": token,
    })
    if updated.rowcount != 1:
        raise RuntimeError("city_public_task_lease_lost")
    released = db.execute(text("""UPDATE city_provider_admission SET active_task_key=NULL,
        active_lease_until=NULL,active_lease_token=NULL
        WHERE singleton=TRUE AND active_task_key=:key AND active_lease_token=:token"""), {
        "key": key, "token": token,
    })
    if released.rowcount != 1:
        raise RuntimeError("city_public_task_lease_lost")


def _perform_public_request(engine: Engine, task: dict[str, Any]) -> None:
    from backend.app.city_geography_provider import get_city_geography_provider, CityGeographyError
    from backend.app.map_imports import submit_osm_import

    try:
        provider = get_city_geography_provider()
        if provider is None:
            _finish_public_request(engine, task, error_code="city_provider_disabled")
            return
        if task["task_kind"] == "world":
            result = provider.fetch_world_catalog()
            _finish_public_request(engine, task, result=result)
        elif task["task_kind"] == "country":
            result = provider.fetch_country_catalog(str(task["country_code"]), list(task["admin_levels"]))
            _finish_public_request(engine, task, result=result)
        else:
            result = provider.fetch_city_extract(int(task["relation_id"]))
            payload = result["xml"]
            if not isinstance(payload, bytes) or len(payload) > task["max_bytes"]:
                raise CityGeographyError("city_provider_response_limit")
            with tempfile.TemporaryDirectory(prefix="city-provision-") as directory:
                source = Path(directory) / "city.osm"
                source.write_bytes(payload)
                submitted = submit_osm_import(
                    engine, source, region=f"auto:{task['country_code']}:{task['relation_id']}",
                    city_relation_ids=[int(task["relation_id"])],
                    source_timestamp=result["source_timestamp"], coverage_mode="complete",
                    coverage_evidence="Complete public city relation extract with named highways.",
                    _transaction_hook=lambda db, map_task_id, checksum, timestamp: _finish_city_import(
                        db, task, map_task_id, checksum, timestamp),
                )
    except CityGeographyError as error:
        code = str(error) if str(error).replace("_", "").isalnum() else "city_provider_unavailable"
        try:
            _finish_public_request(engine, task, error_code=code)
        except RuntimeError as stale:
            if str(stale) != "city_public_task_lease_lost":
                raise
    except Exception as error:
        # A stale import cannot be linked; its public task is reclaimed by lease recovery.
        if isinstance(error, RuntimeError) and str(error) == "city_public_task_lease_lost":
            return
        try:
            _finish_public_request(engine, task, error_code="city_provider_unavailable")
        except RuntimeError as stale:
            if str(stale) != "city_public_task_lease_lost":
                raise


def process_city_provisioning_step(engine: Engine) -> bool:
    """Advance one bounded owner checkpoint or one globally admitted public request."""
    if not enabled():
        return _reconcile_disabled_requests(engine)
    if _prepare_one_owner_step(engine):
        return True
    # Enrollment is an independent bounded lane: a blocked public request must not
    # prevent previously imported sources from entering preparation.
    if _enroll_existing_sources(engine):
        return True
    task = _reserve_public_request(engine)
    if task is None:
        return False
    _perform_public_request(engine, task)
    return True


def create_city_provisioning_router(engine: Engine, current_user) -> APIRouter:
    router = APIRouter()

    @router.get("/api/coverage-preparation", response_model=CoveragePreparationResponse)
    def status(response: Response, account_id: str = Depends(current_user)):
        with engine.begin() as db:
            result = get_coverage_preparation(db, account_id)
        response.headers["Cache-Control"] = "no-store, private"
        return result

    @router.post("/api/coverage-preparation/retry", status_code=204)
    def retry(account_id: str = Depends(current_user)):
        with engine.begin() as db:
            retry_coverage_preparation(db, account_id)
        return Response(status_code=204)

    return router
