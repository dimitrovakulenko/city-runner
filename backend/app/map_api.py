"""Bounded owner-only activity and active-geography viewport APIs."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import Engine

MAX_BBOX_WIDTH = 45.0
MAX_BBOX_HEIGHT = 45.0
MAX_BBOX_AREA = 500.0
MAX_CITIES = 100
MAX_DATASETS = 20
MAX_TRACKS = 50
MAX_TRACK_SOURCE_POINTS = 20000
MAX_STREETS = 200
MAX_STREET_SOURCE_POINTS = 20000
MAX_NODES = 1000
MAX_FEATURE_POINTS = 500
MAX_RESPONSE_POINTS = 20000
MAX_GEOMETRY_BYTES = 1_000_000
NODE_ZOOM = 16
STATEMENT_TIMEOUT_MS = 1500


class LayerLimit(BaseModel):
    returned: int
    limit: int
    truncated: bool


class DatasetStatus(BaseModel):
    id: str
    region: str
    state: Literal["ready", "pending", "failed", "not-matched"]
    progress_revision: str | None
    visited_node_count: int | None
    unsupported_sample_count: int | None
    pending_sources: int | None
    failed_sources: int | None


class CityScope(BaseModel):
    id: str
    dataset_id: str
    name: str
    bounds: tuple[float, float, float, float]


class TrackFeature(BaseModel):
    activity_id: str
    name: str
    date: str
    geometry: dict[str, Any]


class StreetFeature(BaseModel):
    street_id: str
    way_id: str
    dataset_id: str
    city_id: str
    name: str
    geometry: dict[str, Any]
    visited_nodes: int | None
    eligible_nodes: int | None
    completed: bool | None


class MissingNode(BaseModel):
    node_id: str
    dataset_id: str
    longitude: float
    latitude: float


class MapLimits(BaseModel):
    tracks: LayerLimit
    streets: LayerLimit
    missing_nodes: LayerLimit
    cities: LayerLimit
    points: LayerLimit
    geometry_bytes: LayerLimit


class MapResponse(BaseModel):
    bbox: tuple[float, float, float, float]
    zoom: float
    geography_state: Literal["supported", "geography_pending"]
    pending_imports: int
    dataset_truncated: bool
    datasets: list[DatasetStatus]
    cities: list[CityScope]
    tracks: list[TrackFeature]
    streets: list[StreetFeature]
    missing_nodes: list[MissingNode]
    node_state: Literal["ready", "pending", "not-requested", "geography_pending"]
    limits: MapLimits


class ProgressDataset(BaseModel):
    dataset_id: str
    region: str
    state: Literal["ready", "pending", "failed", "not-matched"]
    progress_revision: str | None
    visited_node_count: int | None
    unsupported_sample_count: int | None
    pending_sources: int | None
    failed_sources: int | None
    eligible_streets: int | None
    completed_streets: int | None
    eligible_nodes: int | None


class ProgressResponse(BaseModel):
    state: Literal["ready", "pending", "failed", "not-matched", "unsupported-geography"]
    rule: Literal["normal", "strict"]
    datasets: list[ProgressDataset]
    datasets_truncated: bool
    unmapped_points: int
    pending_imports: int


class BBox(BaseModel):
    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, value: str) -> "BBox":
        try:
            parts = [float(part.strip()) for part in value.split(",")]
        except ValueError as error:
            raise ValueError("bbox must be west,south,east,north") from error
        if len(parts) != 4 or not all(math.isfinite(part) for part in parts):
            raise ValueError("bbox must contain four finite numbers")
        west, south, east, north = parts
        if not (-180 <= west <= 180 and -180 <= east <= 180):
            raise ValueError("bbox longitude must be within [-180,180]")
        if not (-90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("bbox latitude must be within [-90,90]")
        if west >= east:
            raise ValueError("bbox must not wrap the antimeridian and west must be less than east")
        if south >= north:
            raise ValueError("bbox south must be less than north")
        if east - west > MAX_BBOX_WIDTH or north - south > MAX_BBOX_HEIGHT:
            raise ValueError("bbox exceeds the maximum viewport size")
        if (east - west) * (north - south) > MAX_BBOX_AREA:
            raise ValueError("bbox exceeds the maximum viewport area")
        return cls(west=west, south=south, east=east, north=north)

    @property
    def values(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north


def create_map_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/map", response_model=MapResponse)
    def map_view(
        bbox: str = Query(min_length=7, max_length=100),
        zoom: float = Query(ge=0, le=24),
        rule: Literal["normal", "strict"] = Query("normal"),
        account_id: str = Depends(current_user),
    ):
        try:
            bounds = BBox.parse(bbox)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not math.isfinite(zoom):
            raise HTTPException(status_code=422, detail="zoom must be finite")
        params = _bbox_params(bounds)
        tolerance = min(0.01, max(0.000002, 0.0003 * (2 ** (12 - zoom))))
        with _query_connection(engine) as db:
            with db.begin():
                db.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
                city_rows = db.execute(text(_CITY_QUERY), {**params, "limit": MAX_CITIES + 1}).mappings().all()
                pending_imports = _pending_import_count(db, account_id)
                city_truncated = len(city_rows) > MAX_CITIES
                city_rows = city_rows[:MAX_CITIES]
                dataset_ids = list(dict.fromkeys(row["dataset_id"] for row in city_rows))
                dataset_truncated = len(dataset_ids) > MAX_DATASETS
                dataset_ids = dataset_ids[:MAX_DATASETS]
                city_rows = [row for row in city_rows if row["dataset_id"] in dataset_ids]
                dataset_statuses = [
                    _dataset_status(_get_account_coverage(db, account_id, dataset_id), dataset_id,
                                    next(row["region"] for row in city_rows if row["dataset_id"] == dataset_id))
                    for dataset_id in dataset_ids
                ]
                readiness = {int(item.id): item.state for item in dataset_statuses}
                cities = [CityScope(id=str(r["city_id"]), dataset_id=str(r["dataset_id"]), name=r["name"],
                                    bounds=(r["west"], r["south"], r["east"], r["north"]))
                          for r in city_rows]

                raw_tracks = db.execute(text(_TRACK_QUERY), {
                    **params, "account_id": account_id, "limit": MAX_TRACKS + 1,
                    "max_points": MAX_FEATURE_POINTS, "max_source_points": MAX_TRACK_SOURCE_POINTS,
                    "tolerance": tolerance,
                }).mappings().all()
                track_truncated = len(raw_tracks) > MAX_TRACKS
                raw_tracks = raw_tracks[:MAX_TRACKS]
                tracks, used_points, used_bytes, track_dropped = _append_geometry_features(
                    raw_tracks, "geometry_json", MAX_RESPONSE_POINTS, MAX_GEOMETRY_BYTES,
                    initial_points=0, initial_bytes=0,
                    make=lambda row, geometry: TrackFeature(activity_id=str(row["id"]),
                        name=row["name"], date=row["date"] or "unknown", geometry=geometry),
                )
                track_truncated = track_truncated or track_dropped or any(
                    r["point_count"] is None or r["point_count"] > MAX_FEATURE_POINTS for r in raw_tracks)

                raw_streets = []
                street_truncated = False
                missing_nodes = []
                missing_truncated = False
                node_state = "not-requested"
                if dataset_ids:
                    raw_streets = db.execute(text(_STREET_QUERY), {
                        **params, "dataset_ids": dataset_ids, "limit": MAX_STREETS + 1,
                        "tolerance": tolerance, "max_points": MAX_FEATURE_POINTS,
                        "max_source_points": MAX_STREET_SOURCE_POINTS,
                    }).mappings().all()
                    street_truncated = len(raw_streets) > MAX_STREETS
                    raw_streets = raw_streets[:MAX_STREETS]
                visited_by_street = _street_visit_counts(db, account_id, raw_streets)
                street_rows = []
                for row in raw_streets:
                    enriched = dict(row)
                    enriched["visited_nodes"] = visited_by_street.get((row["dataset_id"], row["street_id"]), 0)
                    street_rows.append(enriched)
                streets, used_points, used_bytes, street_dropped = _append_geometry_features(
                    street_rows, "geometry_json", MAX_RESPONSE_POINTS, MAX_GEOMETRY_BYTES,
                    initial_points=used_points, initial_bytes=used_bytes,
                    make=lambda row, geometry: _street_feature(row, geometry, readiness.get(int(row["dataset_id"])), rule),
                )
                street_truncated = street_truncated or street_dropped or any(
                    r["point_count"] is None or r["point_count"] > MAX_FEATURE_POINTS for r in raw_streets)

                if zoom >= NODE_ZOOM and dataset_ids:
                    ready_ids = [dataset_id for dataset_id in dataset_ids if readiness.get(int(dataset_id)) in {"ready", "not-matched"}]
                    pending_ids = len(ready_ids) != len(dataset_ids)
                    node_state = "pending" if pending_ids else "ready"
                    if ready_ids:
                        node_rows = db.execute(text(_MISSING_NODE_QUERY), {
                            **params, "dataset_ids": ready_ids, "account_id": account_id,
                            "limit": MAX_NODES + 1,
                        }).mappings().all()
                        missing_truncated = len(node_rows) > MAX_NODES
                        for row in node_rows[:MAX_NODES]:
                            coordinate_bytes = len(f"[{row['longitude']},{row['latitude']}],".encode("ascii"))
                            if used_points >= MAX_RESPONSE_POINTS or used_bytes + coordinate_bytes > MAX_GEOMETRY_BYTES:
                                missing_truncated = True
                                continue
                            missing_nodes.append(MissingNode(node_id=str(row["node_id"]),
                                dataset_id=str(row["dataset_id"]), longitude=row["longitude"],
                                latitude=row["latitude"]))
                            used_points += 1
                            used_bytes += coordinate_bytes
                elif not dataset_ids:
                    node_state = "geography_pending"

                if city_truncated:
                    city_rows = city_rows[:MAX_CITIES]
                datasets = dataset_statuses
        geography_state = "supported" if dataset_statuses else "geography_pending"
        return MapResponse(
            bbox=bounds.values, zoom=zoom, geography_state=geography_state,
            pending_imports=pending_imports,
            dataset_truncated=dataset_truncated, datasets=datasets, cities=cities,
            tracks=tracks, streets=streets, missing_nodes=missing_nodes, node_state=node_state,
            limits=MapLimits(
                tracks=LayerLimit(returned=len(tracks), limit=MAX_TRACKS, truncated=track_truncated),
                streets=LayerLimit(returned=len(streets), limit=MAX_STREETS, truncated=street_truncated),
                missing_nodes=LayerLimit(returned=len(missing_nodes), limit=MAX_NODES, truncated=missing_truncated),
                cities=LayerLimit(returned=len(cities), limit=MAX_CITIES, truncated=city_truncated),
                points=LayerLimit(returned=used_points, limit=MAX_RESPONSE_POINTS,
                                  truncated=track_truncated or street_truncated or missing_truncated),
                geometry_bytes=LayerLimit(returned=used_bytes, limit=MAX_GEOMETRY_BYTES,
                                          truncated=track_truncated or street_truncated or missing_truncated),
            ),
        )

    @router.get("/api/progress", response_model=ProgressResponse)
    def progress(rule: Literal["normal", "strict"] = Query("normal"),
                 account_id: str = Depends(current_user)):
        with _query_connection(engine) as db:
            with db.begin():
                db.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
                rows = db.execute(text("""
                    SELECT id,region FROM map_datasets
                    WHERE status='active' AND coverage_mode='complete'
                    ORDER BY region,id LIMIT :limit
                """), {"limit": MAX_DATASETS + 1}).mappings().all()
                datasets_truncated = len(rows) > MAX_DATASETS
                rows = rows[:MAX_DATASETS]
                result = []
                for row in rows:
                    dataset_id = int(row["id"])
                    coverage = _get_account_coverage(db, account_id, dataset_id)
                    status = _dataset_status(coverage, dataset_id, row["region"])
                    ready = status.state in {"ready", "not-matched"}
                    counts = _progress_counts(db, account_id, dataset_id, rule) if ready else None
                    result.append(ProgressDataset(
                        dataset_id=str(dataset_id), region=row["region"], state=status.state,
                        progress_revision=status.progress_revision,
                        visited_node_count=status.visited_node_count,
                        unsupported_sample_count=status.unsupported_sample_count,
                        pending_sources=status.pending_sources, failed_sources=status.failed_sources,
                        eligible_streets=counts["eligible_streets"] if counts else None,
                        completed_streets=counts["completed_streets"] if counts else None,
                        eligible_nodes=counts["eligible_nodes"] if counts else None,
                    ))
                unmapped = db.execute(text("""
                    SELECT COALESCE(sum(unmapped_points),0) FROM activities WHERE user_id=:account
                """), {"account": account_id}).scalar_one()
                pending_imports = _pending_import_count(db, account_id)
        if not result:
            state = "unsupported-geography"
        elif any(item.state == "failed" for item in result):
            state = "failed"
        elif any(item.state == "pending" for item in result):
            state = "pending"
        elif all(item.state == "not-matched" for item in result):
            state = "not-matched"
        else:
            state = "ready"
        return ProgressResponse(state=state, rule=rule, datasets=result,
                                datasets_truncated=datasets_truncated, unmapped_points=int(unmapped or 0),
                                pending_imports=pending_imports)

    return router


def _dataset_status(coverage: dict[str, Any], dataset_id: int, region: str) -> DatasetStatus:
    status = coverage.get("status", "pending")
    visited = int(coverage.get("visited_node_count") or 0)
    unsupported = int(coverage.get("unsupported_sample_count") or 0)
    if status == "ready" and visited == 0 and unsupported > 0:
        state = "not-matched"
    elif status in {"ready", "pending", "failed"}:
        state = status
    else:
        state = "pending"
    ready = state in {"ready", "not-matched"}
    return DatasetStatus(
        id=str(dataset_id), region=region, state=state,
        progress_revision=str(coverage["progress_revision"]) if coverage.get("progress_revision") is not None else None,
        visited_node_count=visited if ready else None,
        unsupported_sample_count=unsupported if ready else None,
        pending_sources=int(coverage.get("pending_sources") or 0),
        failed_sources=int(coverage.get("failed_sources") or 0),
    )


def _get_account_coverage(db, account_id: str, dataset_id: int) -> dict[str, Any]:
    from backend.app.coverage import get_account_coverage
    return get_account_coverage(db, account_id, dataset_id)


def _pending_import_count(db, account_id: str) -> int:
    return int(db.execute(text("""
        SELECT count(*) FROM activity_sources
        WHERE account_id=:account AND status IN ('queued','processing')
    """), {"account": account_id}).scalar_one())


def _street_visit_counts(db, account_id: str, rows) -> dict[tuple[int, int], int]:
    dataset_ids = list({int(row["dataset_id"]) for row in rows})
    street_ids = list({int(row["street_id"]) for row in rows})
    if not dataset_ids or not street_ids:
        return {}
    visits = db.execute(text(_STREET_VISIT_QUERY), {
        "account_id": account_id, "dataset_ids": dataset_ids, "street_ids": street_ids,
    }).mappings().all()
    return {(row["dataset_id"], row["street_id"]): row["visited_nodes"] for row in visits}


def _street_feature(row, geometry, state, rule):
    ready = state in {"ready", "not-matched"}
    visited = row["visited_nodes"] if ready else None
    eligible = row["eligible_nodes"] if ready else None
    threshold = (eligible if rule == "strict" or eligible < 10 else math.ceil(eligible * 0.9)) if ready else None
    return StreetFeature(
        street_id=str(row["street_id"]), dataset_id=str(row["dataset_id"]), city_id=str(row["city_id"]),
        way_id=str(row["way_id"]),
        name=row["name"], geometry=geometry, visited_nodes=visited, eligible_nodes=eligible,
        completed=bool(visited >= threshold) if ready else None,
    )


def _append_geometry_features(rows, geometry_column, max_points, max_bytes, *,
                              initial_points, initial_bytes, make):
    result = []
    points, size = initial_points, initial_bytes
    dropped = False
    for row in rows:
        if row["point_count"] is None or row["point_count"] > MAX_FEATURE_POINTS or row[geometry_column] is None:
            dropped = True
            continue
        raw = row[geometry_column]
        geometry = json.loads(raw) if isinstance(raw, str) else raw
        feature_bytes = len(raw.encode("utf-8")) if isinstance(raw, str) else len(json.dumps(raw, separators=(",", ":")).encode())
        count = int(row["point_count"])
        if points + count > max_points or size + feature_bytes > max_bytes:
            dropped = True
            continue
        result.append(make(row, geometry))
        points += count
        size += feature_bytes
    return result, points, size, dropped


def _bbox_params(bounds: BBox) -> dict[str, float]:
    return {"west": bounds.west, "south": bounds.south, "east": bounds.east, "north": bounds.north}


@contextmanager
def _query_connection(engine):
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            yield db
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
        if sqlstate == "57014":
            raise HTTPException(status_code=503,
                detail="Spatial query exceeded its work limit; retry with a smaller viewport.") from error
        raise


_CITY_QUERY = """
    WITH box AS (SELECT ST_MakeEnvelope(:west,:south,:east,:north,4326) AS geom)
    SELECT d.id AS dataset_id,d.region,c.id AS city_id,substring(c.name,1,200) AS name,
           ST_XMin(Box2D(c.boundary)) AS west,ST_YMin(Box2D(c.boundary)) AS south,
           ST_XMax(Box2D(c.boundary)) AS east,ST_YMax(Box2D(c.boundary)) AS north
    FROM map_datasets d JOIN cities c ON c.dataset_id=d.id CROSS JOIN box
    WHERE d.status='active' AND d.coverage_mode='complete'
      AND c.boundary && box.geom AND ST_Intersects(c.boundary,box.geom)
    ORDER BY d.region,d.id,c.name,c.id LIMIT :limit
"""


_TRACK_QUERY = """
    WITH box AS (SELECT ST_MakeEnvelope(:west,:south,:east,:north,4326) AS geom),
    candidates AS MATERIALIZED (
      SELECT a.id,a.name,a.date,a.track_geometry,ST_NPoints(a.track_geometry) AS source_points
      FROM activities a CROSS JOIN box
      WHERE a.user_id=:account_id AND a.track_geometry IS NOT NULL
        AND a.track_geometry && box.geom
      ORDER BY a.id DESC LIMIT :limit
    ),
    clipped AS (
      SELECT candidates.id,candidates.name,candidates.date,candidates.source_points,
        CASE WHEN candidates.source_points<=:max_source_points THEN
          CASE WHEN ST_Intersects(candidates.track_geometry,box.geom) THEN
            ST_SimplifyPreserveTopology(ST_Intersection(candidates.track_geometry,box.geom),:tolerance)
          END
        END AS geom
      FROM candidates CROSS JOIN box
    )
    SELECT id,name,date,COALESCE(ST_NPoints(geom),source_points) AS point_count,
      CASE WHEN source_points<=:max_source_points AND NOT ST_IsEmpty(geom) AND ST_NPoints(geom)<=:max_points
        THEN ST_AsGeoJSON(geom,6) END AS geometry_json
    FROM clipped ORDER BY id DESC
"""


_STREET_QUERY = """
    WITH box AS (SELECT ST_MakeEnvelope(:west,:south,:east,:north,4326) AS geom),
    candidates AS MATERIALIZED (
      SELECT s.id AS street_id,s.dataset_id,s.city_id,substring(s.display_name,1,200) AS name,
        s.eligible_node_count AS eligible_nodes,
        w.geometry AS source_geom,c.boundary AS city_geom,ST_NPoints(w.geometry) AS source_points,
        sw.osm_way_id
      FROM streets s JOIN street_ways sw ON sw.dataset_id=s.dataset_id AND sw.street_id=s.id
      JOIN osm_ways w ON w.dataset_id=sw.dataset_id AND w.osm_way_id=sw.osm_way_id
      JOIN cities c ON c.dataset_id=s.dataset_id AND c.id=s.city_id
      CROSS JOIN box
      WHERE s.dataset_id=ANY(:dataset_ids) AND s.eligible_node_count>0
        AND c.boundary && box.geom AND w.geometry && box.geom
      ORDER BY s.dataset_id,s.city_id,s.id,sw.osm_way_id LIMIT :limit
    ), clipped AS (
      SELECT candidates.*,
        CASE WHEN source_points<=:max_source_points THEN
          CASE WHEN ST_Intersects(source_geom,box.geom) AND ST_Intersects(source_geom,city_geom) THEN
            ST_SimplifyPreserveTopology(
              ST_Intersection(ST_Intersection(source_geom,city_geom),box.geom),:tolerance)
          END
        END AS geom
      FROM candidates CROSS JOIN box
    )
    SELECT street_id,dataset_id,city_id,name,eligible_nodes,osm_way_id AS way_id,
      COALESCE(ST_NPoints(geom),source_points) AS point_count,
      CASE WHEN source_points<=:max_source_points AND NOT ST_IsEmpty(geom) AND ST_NPoints(geom)<=:max_points
        THEN ST_AsGeoJSON(geom,6) END AS geometry_json
    FROM clipped ORDER BY dataset_id,city_id,street_id,osm_way_id
"""


_MISSING_NODE_QUERY = """
    WITH box AS (SELECT ST_MakeEnvelope(:west,:south,:east,:north,4326) AS geom),
    missing AS (
      SELECT sn.dataset_id,sn.osm_node_id,n.point::geometry AS geom
      FROM street_nodes sn JOIN osm_nodes n
        ON n.dataset_id=sn.dataset_id AND n.osm_node_id=sn.osm_node_id
      CROSS JOIN box
      WHERE sn.dataset_id=ANY(:dataset_ids) AND n.point && box.geom::geography
        AND ST_Covers(box.geom,n.point::geometry)
        AND NOT EXISTS (
          SELECT 1 FROM source_node_contributions c
          JOIN activity_sources src ON src.account_id=c.account_id AND src.id=c.source_id
            AND src.revision=c.source_revision AND src.status='succeeded' AND src.activity_id IS NOT NULL
          JOIN coverage_source_runs r ON r.account_id=c.account_id AND r.source_id=c.source_id
            AND r.source_revision=c.source_revision AND r.dataset_id=c.dataset_id AND r.status='succeeded'
          JOIN jobs j ON j.account_id=r.account_id AND j.id=r.job_id AND j.status='succeeded'
          WHERE c.account_id=:account_id AND c.dataset_id=sn.dataset_id AND c.node_id=sn.osm_node_id
        )
      GROUP BY sn.dataset_id,sn.osm_node_id,n.point
      ORDER BY sn.dataset_id,sn.osm_node_id LIMIT :limit
    )
    SELECT dataset_id,osm_node_id AS node_id,ST_X(geom) AS longitude,ST_Y(geom) AS latitude
    FROM missing ORDER BY dataset_id,node_id
"""


def _progress_counts(db, account_id: str, dataset_id: int, rule: str):
    threshold = "s.eligible_node_count" if rule == "strict" else "CASE WHEN s.eligible_node_count < 10 THEN s.eligible_node_count ELSE ceil(s.eligible_node_count*0.9)::integer END"
    return db.execute(text(f"""
        WITH visited AS (
          SELECT sn.dataset_id,sn.street_id,count(DISTINCT c.node_id)::integer AS visited_nodes
          FROM street_nodes sn JOIN source_node_contributions c
            ON c.dataset_id=sn.dataset_id AND c.node_id=sn.osm_node_id AND c.account_id=:account
          JOIN activity_sources src ON src.account_id=c.account_id AND src.id=c.source_id
            AND src.revision=c.source_revision AND src.status='succeeded' AND src.activity_id IS NOT NULL
          JOIN coverage_source_runs r ON r.account_id=c.account_id AND r.source_id=c.source_id
            AND r.source_revision=c.source_revision AND r.dataset_id=c.dataset_id AND r.status='succeeded'
          JOIN jobs j ON j.account_id=r.account_id AND j.id=r.job_id AND j.status='succeeded'
          WHERE sn.dataset_id=:dataset GROUP BY sn.dataset_id,sn.street_id
        )
        SELECT count(*)::integer AS eligible_streets,
          count(*) FILTER (WHERE COALESCE(v.visited_nodes,0) >= {threshold})::integer AS completed_streets,
          (SELECT count(DISTINCT sn.osm_node_id)::bigint FROM street_nodes sn
            WHERE sn.dataset_id=:dataset) AS eligible_nodes
        FROM streets s LEFT JOIN visited v ON v.dataset_id=s.dataset_id AND v.street_id=s.id
        WHERE s.dataset_id=:dataset AND s.eligible_node_count>0
    """), {"account": account_id, "dataset": dataset_id}).mappings().one()


_STREET_VISIT_QUERY = """
    SELECT sn.dataset_id,sn.street_id,count(DISTINCT c.node_id)::integer AS visited_nodes
    FROM street_nodes sn JOIN source_node_contributions c
      ON c.dataset_id=sn.dataset_id AND c.node_id=sn.osm_node_id AND c.account_id=:account_id
    JOIN activity_sources src ON src.account_id=c.account_id AND src.id=c.source_id
      AND src.revision=c.source_revision AND src.status='succeeded' AND src.activity_id IS NOT NULL
    JOIN coverage_source_runs r ON r.account_id=c.account_id AND r.source_id=c.source_id
      AND r.source_revision=c.source_revision AND r.dataset_id=c.dataset_id AND r.status='succeeded'
    JOIN jobs j ON j.account_id=r.account_id AND j.id=r.job_id AND j.status='succeeded'
    WHERE sn.dataset_id=ANY(:dataset_ids) AND sn.street_id=ANY(:street_ids)
    GROUP BY sn.dataset_id,sn.street_id
"""
