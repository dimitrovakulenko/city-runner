"""Bounded pedestrian route preview and owner-scoped saved routes."""

from __future__ import annotations

import json
import math
import os
from decimal import Decimal
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import Engine, text


MAX_CLIENT_REVISION = 2_147_483_647
MAX_ROUTE_POSITIONS = 10_000
MAX_PROVIDER_BYTES = 2 * 1024 * 1024
PROVIDER_TIMEOUT_SECONDS = 8
SNAP_LIMIT_METERS = 100.0
FOSSGIS_ENDPOINT = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/"
FOSSGIS_ATTRIBUTION = {
    "text": "© OpenStreetMap contributors · Routing by FOSSGIS",
    "url": "https://www.openstreetmap.org/copyright/en",
    "fix_map_url": "https://www.openstreetmap.org/fixthemap",
    "waypoints_notice": "Your waypoints are sent to FOSSGIS for routing.",
}
GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def _position_list(value: Any, *, min_count: int, max_count: int, field: str) -> list[list[float]]:
    if not isinstance(value, (list, tuple)) or not min_count <= len(value) <= max_count:
        raise ValueError(f"{field} must contain between {min_count} and {max_count} positions")
    result: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{field} positions must be [longitude, latitude]")
        longitude, latitude = point
        if (isinstance(longitude, bool) or isinstance(latitude, bool)
                or not isinstance(longitude, (int, float)) or not isinstance(latitude, (int, float))
                or not -180 <= longitude <= 180 or not -90 <= latitude <= 90):
            raise ValueError(f"{field} contains an invalid WGS84 coordinate")
        result.append([float(longitude), float(latitude)])
    return result


class RouteAttribution(BaseModel):
    text: str
    url: str
    fix_map_url: str
    waypoints_notice: str


class RouteGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["LineString"]
    coordinates: list[tuple[float, float]]

    @field_validator("coordinates", mode="before")
    @classmethod
    def validate_coordinates(cls, value: Any) -> list[list[float]]:
        return _position_list(value, min_count=2, max_count=MAX_ROUTE_POSITIONS, field="geometry")


class RouteWaypoints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waypoints: list[tuple[float, float]]

    @field_validator("waypoints", mode="before")
    @classmethod
    def validate_waypoints(cls, value: Any) -> list[list[float]]:
        return _position_list(value, min_count=2, max_count=20, field="waypoints")


class RoutePreviewRequest(RouteWaypoints):
    client_revision: StrictInt = Field(ge=0, le=MAX_CLIENT_REVISION)


class RouteCreateRequest(RouteWaypoints):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if any(not (c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
                    or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF) for c in value):
            raise ValueError("name contains a character not allowed in GPX XML")
        return value


class RouteUpdateRequest(RouteCreateRequest):
    expected_revision: StrictInt = Field(ge=1, le=MAX_CLIENT_REVISION)


class RoutePreviewResponse(BaseModel):
    client_revision: StrictInt = Field(ge=0, le=MAX_CLIENT_REVISION)
    geometry: RouteGeometry
    distance_m: float = Field(ge=0, allow_inf_nan=False)
    duration_s: float = Field(ge=0, allow_inf_nan=False)
    provider: str
    attribution: RouteAttribution


class RouteSummary(BaseModel):
    id: str
    name: str
    revision: int
    distance_m: float
    created_at: datetime
    updated_at: datetime


class RouteDetail(RouteSummary):
    waypoints: list[tuple[float, float]]
    geometry: RouteGeometry
    duration_s: float
    provider: str
    attribution: RouteAttribution


class RoutePage(BaseModel):
    items: list[RouteSummary]
    total: int
    limit: int
    offset: int


def _finite_number(value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ValueError("invalid route number")
    try:
        converted = float(value)
    except OverflowError as exc:
        raise ValueError("invalid route number") from exc
    if not math.isfinite(converted):
        raise ValueError("invalid route number")
    return converted


def _distance_m(left: list[float], right: list[float]) -> float:
    radius = 6_371_008.8
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(haversine)))


def _provider_request(waypoints: list[list[float]]) -> Mapping[str, Any]:
    if os.getenv("ROUTING_PROVIDER", "").strip().lower() != "fossgis":
        raise HTTPException(status_code=503, detail="Pedestrian routing is unavailable.")
    coords = ";".join(f"{lon:.8f},{lat:.8f}" for lon, lat in waypoints)
    radiuses = ";".join("100" for _ in waypoints)
    request = urllib.request.Request(
        FOSSGIS_ENDPOINT + coords + f"?overview=full&geometries=geojson&steps=false&radiuses={radiuses}",
        headers={"User-Agent": "CityRunner/1.0 (pedestrian route preview)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_PROVIDER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_PROVIDER_BYTES + 1)
            payload = json.loads(body) if len(body) <= MAX_PROVIDER_BYTES else None
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            payload = None
        error_code = payload.get("code") if isinstance(payload, dict) else None
        if exc.code == 400 and error_code in ("NoRoute", "NoSegment"):
            raise HTTPException(status_code=422, detail="No pedestrian route connects these waypoints.") from exc
        raise HTTPException(status_code=503, detail="Routing provider is unavailable.") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise HTTPException(status_code=504, detail="Routing provider timed out.") from exc
        raise HTTPException(status_code=503, detail="Routing provider is unavailable.") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Routing provider is unavailable.") from exc
    if len(raw) > MAX_PROVIDER_BYTES:
        raise HTTPException(status_code=503, detail="Routing provider response exceeded its limit.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.")
    return value


def _reserve_provider_quota(engine: Engine) -> None:
    with engine.begin() as db:
        allowed = db.execute(text("""UPDATE routing_provider_quota
            SET next_allowed_at=clock_timestamp() + interval '1 second'
            WHERE provider='fossgis' AND next_allowed_at <= clock_timestamp()
            RETURNING next_allowed_at""")).first()
        if allowed is not None:
            return
        delay = db.execute(text("""SELECT GREATEST(1,ceil(extract(epoch FROM
            (next_allowed_at-clock_timestamp())))::integer) FROM routing_provider_quota
            WHERE provider='fossgis'""")).scalar_one_or_none()
        if delay is None:
            raise HTTPException(status_code=503, detail="Routing quota is unavailable.")
    raise HTTPException(status_code=429, detail="Routing provider rate limit reached.",
                        headers={"Retry-After": str(delay)})


def _calculate_route(engine: Engine, waypoints: list[list[float]], client_revision: int,
                     routing_provider: Callable[[list[list[float]]], Mapping[str, Any]] | None = None
                     ) -> RoutePreviewResponse:
    if routing_provider is None:
        if os.getenv("ROUTING_PROVIDER", "").strip().lower() != "fossgis":
            raise HTTPException(status_code=503, detail="Pedestrian routing is unavailable.")
        _reserve_provider_quota(engine)
        result = _provider_request(waypoints)
        provider = "fossgis"
    else:
        # Test-only injection; still pass through the same untrusted response validator.
        result = routing_provider(waypoints)
        provider = "test"
    if not isinstance(result, Mapping):
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.")
    code = result.get("code")
    if not isinstance(code, str):
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.")
    if code in {"NoRoute", "NoSegment"}:
        raise HTTPException(status_code=422, detail="No pedestrian route connects these waypoints.")
    if code != "Ok":
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.")
    try:
        matched = result["waypoints"]
        if not isinstance(matched, list) or len(matched) != len(waypoints):
            raise ValueError("unexpected snapped waypoint count")
        for original, item in zip(waypoints, matched):
            if not isinstance(item, dict):
                raise ValueError("invalid snapped waypoint")
            location = _position_list([item.get("location")], min_count=1, max_count=1, field="location")[0]
            distance = _finite_number(item.get("distance"))
            if distance > SNAP_LIMIT_METERS or _distance_m(original, location) > SNAP_LIMIT_METERS:
                raise HTTPException(status_code=422, detail="A waypoint is too far from the pedestrian network.")
            if not all(math.isfinite(c) for c in location):
                raise ValueError("invalid snapped waypoint")
        routes = result["routes"]
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            raise ValueError("missing route")
        route = routes[0]
        geometry = route["geometry"]
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            raise ValueError("invalid route geometry")
        positions = _position_list(geometry.get("coordinates"), min_count=2,
                                   max_count=MAX_ROUTE_POSITIONS, field="geometry")
        if len({(point[0], point[1]) for point in positions}) < 2:
            raise HTTPException(status_code=422, detail="No pedestrian route connects these waypoints.")
        response = RoutePreviewResponse.model_validate({
            "client_revision": client_revision,
            "geometry": {"type": "LineString", "coordinates": positions},
            "distance_m": _finite_number(route.get("distance")),
            "duration_s": _finite_number(route.get("duration")),
            "provider": provider,
            "attribution": FOSSGIS_ATTRIBUTION,
        })
        return response
    except HTTPException:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Routing provider returned an invalid response.") from exc


def _gpx(name: str, geometry: RouteGeometry) -> bytes:
    ET.register_namespace("", GPX_NAMESPACE)
    root = ET.Element(f"{{{GPX_NAMESPACE}}}gpx", {"version": "1.1", "creator": "City Runner"})
    route = ET.SubElement(root, f"{{{GPX_NAMESPACE}}}rte")
    ET.SubElement(route, f"{{{GPX_NAMESPACE}}}name").text = name
    for longitude, latitude in geometry.coordinates:
        ET.SubElement(route, f"{{{GPX_NAMESPACE}}}rtept", {
            "lat": format(Decimal(repr(latitude)), "f"),
            "lon": format(Decimal(repr(-180.0 if longitude == 180.0 else longitude)), "f"),
        })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _detail(db, account_id: str, route_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""SELECT id,name,revision,waypoints,ST_AsGeoJSON(geometry,17) AS geometry,
        distance_m,duration_s,provider,attribution,created_at,updated_at
        FROM saved_routes WHERE id=:id AND account_id=:account"""), {
        "id": route_id, "account": account_id,
    }).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Route not found.")
    geometry = row["geometry"]
    attribution = row["attribution"]
    waypoints = row["waypoints"]
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    if isinstance(attribution, str):
        attribution = json.loads(attribution)
    if isinstance(waypoints, str):
        waypoints = json.loads(waypoints)
    return {
        "id": str(row["id"]), "name": row["name"], "revision": row["revision"],
        "waypoints": waypoints, "geometry": geometry,
        "distance_m": row["distance_m"], "duration_s": row["duration_s"],
        "provider": row["provider"], "attribution": attribution,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def create_routes_router(engine: Engine, current_user: Callable[..., str],
                         routing_provider: Callable[[list[list[float]]], Mapping[str, Any]] | None = None) -> APIRouter:
    """Create route endpoints; routing_provider is a test-only response fixture hook."""
    router = APIRouter()

    @router.post("/api/routes/preview", response_model=RoutePreviewResponse)
    def preview(body: RoutePreviewRequest, account_id: str = Depends(current_user)):
        return _calculate_route(engine, [list(p) for p in body.waypoints], body.client_revision, routing_provider)

    @router.get("/api/routes", response_model=RoutePage)
    def list_routes(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_CLIENT_REVISION),
                    account_id: str = Depends(current_user)):
        with engine.connect() as db:
            total = db.execute(text("SELECT count(*) FROM saved_routes WHERE account_id=:account"),
                               {"account": account_id}).scalar_one()
            rows = db.execute(text("""SELECT id,name,revision,distance_m,created_at,updated_at
                FROM saved_routes WHERE account_id=:account
                ORDER BY updated_at DESC,id DESC LIMIT :limit OFFSET :offset"""), {
                "account": account_id, "limit": limit, "offset": offset,
            }).mappings().all()
        return {"items": [{**dict(row), "id": str(row["id"])} for row in rows],
                "total": total, "limit": limit, "offset": offset}

    @router.post("/api/routes", status_code=201, response_model=RouteDetail)
    def create_route(body: RouteCreateRequest, account_id: str = Depends(current_user)):
        calculated = _calculate_route(engine, [list(p) for p in body.waypoints], 0, routing_provider)
        route_id = uuid.uuid4()
        with engine.begin() as db:
            db.execute(text("""INSERT INTO saved_routes
                (id,account_id,name,revision,waypoints,geometry,distance_m,duration_s,provider,attribution,routed_at)
                VALUES (:id,:account,:name,1,CAST(:waypoints AS jsonb),
                    ST_SetSRID(ST_GeomFromGeoJSON(:geometry),4326),:distance,:duration,
                    :provider,CAST(:attribution AS jsonb),clock_timestamp())"""), {
                "id": route_id, "account": account_id, "name": body.name,
                "waypoints": json.dumps([list(p) for p in body.waypoints]),
                "geometry": calculated.geometry.model_dump_json(),
                "distance": calculated.distance_m, "duration": calculated.duration_s,
                "provider": calculated.provider, "attribution": calculated.attribution.model_dump_json(),
            })
            return _detail(db, account_id, route_id)

    @router.get("/api/routes/{route_id}", response_model=RouteDetail)
    def get_route(route_id: UUID, account_id: str = Depends(current_user)):
        with engine.connect() as db:
            return _detail(db, account_id, route_id)

    @router.put("/api/routes/{route_id}", response_model=RouteDetail)
    def update_route(route_id: UUID, body: RouteUpdateRequest, account_id: str = Depends(current_user)):
        new_waypoints = [list(p) for p in body.waypoints]
        with engine.connect() as db:
            existing = db.execute(text("""SELECT revision,waypoints FROM saved_routes
                WHERE id=:id AND account_id=:account"""), {"id": route_id, "account": account_id}).mappings().first()
        if existing is None:
            raise HTTPException(status_code=404, detail="Route not found.")
        if existing["revision"] != body.expected_revision:
            raise HTTPException(status_code=409, detail="Route has changed; reload before saving.")
        if body.expected_revision >= MAX_CLIENT_REVISION:
            raise HTTPException(status_code=409, detail="Route revision cannot be increased further.")
        old_waypoints = existing["waypoints"]
        if isinstance(old_waypoints, str):
            old_waypoints = json.loads(old_waypoints)
        calculated = None
        if old_waypoints != new_waypoints:
            calculated = _calculate_route(engine, new_waypoints, 0, routing_provider)
        params: dict[str, Any] = {
            "id": route_id, "account": account_id, "revision": body.expected_revision, "name": body.name,
        }
        if calculated is None:
            sql = """UPDATE saved_routes SET name=:name,revision=revision+1,updated_at=clock_timestamp()
                WHERE id=:id AND account_id=:account AND revision=:revision"""
        else:
            params.update({
                "waypoints": json.dumps(new_waypoints), "geometry": calculated.geometry.model_dump_json(),
                "distance": calculated.distance_m, "duration": calculated.duration_s,
                "provider": calculated.provider, "attribution": calculated.attribution.model_dump_json(),
            })
            sql = """UPDATE saved_routes SET name=:name,revision=revision+1,waypoints=CAST(:waypoints AS jsonb),
                geometry=ST_SetSRID(ST_GeomFromGeoJSON(:geometry),4326),distance_m=:distance,duration_s=:duration,
                provider=:provider,attribution=CAST(:attribution AS jsonb),routed_at=clock_timestamp(),
                updated_at=clock_timestamp()
                WHERE id=:id AND account_id=:account AND revision=:revision"""
        with engine.begin() as db:
            result = db.execute(text(sql), params)
            if result.rowcount != 1:
                exists = db.execute(text("SELECT 1 FROM saved_routes WHERE id=:id AND account_id=:account"), {
                    "id": route_id, "account": account_id,
                }).first()
                if exists is None:
                    raise HTTPException(status_code=404, detail="Route not found.")
                raise HTTPException(status_code=409, detail="Route has changed; reload before saving.")
            return _detail(db, account_id, route_id)

    @router.delete("/api/routes/{route_id}", status_code=204)
    def delete_route(route_id: UUID, expected_revision: int = Query(ge=1, le=MAX_CLIENT_REVISION),
                     account_id: str = Depends(current_user)):
        with engine.begin() as db:
            result = db.execute(text("DELETE FROM saved_routes WHERE id=:id AND account_id=:account AND revision=:revision"), {
                "id": route_id, "account": account_id, "revision": expected_revision,
            })
            if result.rowcount != 1:
                exists = db.execute(text("SELECT 1 FROM saved_routes WHERE id=:id AND account_id=:account"), {
                    "id": route_id, "account": account_id,
                }).first()
                if exists is None:
                    raise HTTPException(status_code=404, detail="Route not found.")
                raise HTTPException(status_code=409, detail="Route has changed; reload before deleting.")
        return Response(status_code=204)

    @router.get("/api/routes/{route_id}/gpx")
    def export_route(route_id: UUID, account_id: str = Depends(current_user)):
        with engine.connect() as db:
            detail = _detail(db, account_id, route_id)
        filename = f"route-{route_id}.gpx"
        return Response(content=_gpx(detail["name"], RouteGeometry.model_validate(detail["geometry"])),
                        media_type="application/gpx+xml; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    return router
