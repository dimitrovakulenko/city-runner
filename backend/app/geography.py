"""Import versioned, public OSM geography and expose bounded lookup helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from defusedxml import ElementTree
from sqlalchemy import create_engine, text


ELIGIBILITY_RULE_VERSION = "named-highway-city-name-v1"
EXCLUDED_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "construction", "proposed", "raceway",
}
DEFAULT_NEARBY_RADIUS_M = 25.0
MAX_NEARBY_RADIUS_M = 1000.0
MAX_LOOKUP_ROWS = 500


@dataclass(frozen=True)
class Way:
    osm_id: int
    nodes: tuple[int, ...]
    tags: dict[str, str]


@dataclass(frozen=True)
class Relation:
    osm_id: int
    tags: dict[str, str]
    members: tuple[tuple[str, int, str], ...]


@dataclass(frozen=True)
class OSMDocument:
    nodes: dict[int, tuple[float, float]]
    ways: dict[int, Way]
    relations: dict[int, Relation]
    source_timestamp: str | None


def parse_osm_xml(path: str | Path, city_relation_ids: set[int]) -> OSMDocument:
    """Read a regional OSM XML extract; relation members must be complete."""
    with Path(path).open("rb") as source:
        return _parse_osm_stream(source, city_relation_ids)


def _parse_osm_stream(source, city_relation_ids: set[int]) -> OSMDocument:
    root = ElementTree.parse(source).getroot()
    if root.tag != "osm":
        raise ValueError("Expected an OSM XML document with an <osm> root.")

    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, Way] = {}
    relations: dict[int, Relation] = {}
    for element in root:
        if element.tag == "node":
            osm_id = _positive_id(element.get("id"), "node")
            lon = _coordinate(element.get("lon"), -180, 180, "longitude")
            lat = _coordinate(element.get("lat"), -90, 90, "latitude")
            if osm_id in nodes and nodes[osm_id] != (lon, lat):
                raise ValueError(f"Conflicting duplicate OSM node {osm_id}.")
            nodes[osm_id] = (lon, lat)
        elif element.tag == "way":
            osm_id = _positive_id(element.get("id"), "way")
            refs = tuple(_positive_id(node.get("ref"), "way node") for node in element.findall("nd"))
            tags = _tags(element)
            way = Way(osm_id, refs, tags)
            previous = ways.get(osm_id)
            if previous is not None:
                if previous.nodes != way.nodes or any(
                    key in previous.tags and previous.tags[key] != value
                    for key, value in way.tags.items()
                ):
                    raise ValueError(f"Conflicting duplicate OSM way {osm_id}.")
                way = Way(osm_id, refs, {**previous.tags, **tags})
            ways[osm_id] = way
        elif element.tag == "relation":
            osm_id = _positive_id(element.get("id"), "relation")
            if osm_id not in city_relation_ids:
                continue
            tags = _tags(element)
            members = tuple(
                (member.get("type", ""), _positive_id(member.get("ref"), "relation member"),
                 member.get("role", ""))
                for member in element.findall("member")
            )
            relations[osm_id] = Relation(osm_id, tags, members)

    missing = city_relation_ids - relations.keys()
    if missing:
        raise ValueError(f"Selected city relation(s) missing from extract: {sorted(missing)}")
    meta = root.find("meta")
    timestamp = (root.get("timestamp") or root.get("osmosis_replication_timestamp")
                 or (meta.get("osm_base") if meta is not None else None))
    return OSMDocument(nodes, ways, relations, timestamp)


def eligible_way(tags: dict[str, str]) -> bool:
    """Match the PoC's named-highway/access/foot exclusions exactly."""
    highway = tags.get("highway")
    return bool(
        tags.get("name")
        and highway
        and tags.get("access") not in {"private", "no"}
        and tags.get("foot") != "no"
        and highway not in EXCLUDED_HIGHWAYS
    )


def _positive_id(value: str | None, what: str) -> int:
    try:
        result = int(value or "")
    except ValueError as exc:
        raise ValueError(f"Invalid OSM {what} ID.") from exc
    if result <= 0:
        raise ValueError(f"OSM {what} ID must be positive.")
    return result


def _coordinate(value: str | None, low: float, high: float, name: str) -> float:
    try:
        result = float(value or "nan")
    except ValueError as exc:
        raise ValueError(f"Invalid OSM {name}.") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"OSM {name} is outside valid WGS84 bounds.")
    return result


def _tags(element) -> dict[str, str]:
    result = {}
    for item in element.findall("tag"):
        key, value = item.get("k"), item.get("v")
        if key is None or value is None:
            raise ValueError("OSM tags require key and value.")
        result[key] = value
    return result


def _stitch_rings(relation: Relation, ways: dict[int, Way], nodes: dict[int, tuple[float, float]]):
    grouped: dict[str, list[tuple[int, ...]]] = {"outer": [], "inner": []}
    for member_type, way_id, role in relation.members:
        if role in {"label", "admin_centre", "subarea"}:
            continue
        if role not in {"", "outer", "inner"}:
            raise ValueError(f"Boundary relation {relation.osm_id} has unsupported member role {role!r}.")
        if member_type != "way":
            raise ValueError(f"Boundary relation {relation.osm_id} has non-way {role or 'unlabelled'} geometry member.")
        if way_id not in ways:
            raise ValueError(f"Boundary relation {relation.osm_id} references missing way {way_id}.")
        refs = ways[way_id].nodes
        if len(refs) < 2 or any(node_id not in nodes for node_id in refs):
            raise ValueError(f"Boundary relation {relation.osm_id} has incomplete way {way_id}.")
        grouped["inner" if role == "inner" else "outer"].append(refs)

    result = {}
    for role, segments in grouped.items():
        rings = []
        while segments:
            ring = list(segments.pop(0))
            while ring[-1] != ring[0]:
                match = None
                for index, segment in enumerate(segments):
                    if segment[0] == ring[-1]:
                        match = (index, segment)
                        break
                    if segment[-1] == ring[-1]:
                        match = (index, tuple(reversed(segment)))
                        break
                if match is None:
                    raise ValueError(f"Boundary relation {relation.osm_id} has an unclosed {role} ring.")
                index, segment = match
                ring.extend(segment[1:])
                segments.pop(index)
                if len(ring) > sum(len(part) for part in segments) + 1_000_000:
                    raise ValueError("Boundary ring is unreasonably large.")
            if len(ring) < 4 or len(set(ring[:-1])) < 3:
                raise ValueError(f"Boundary relation {relation.osm_id} has a degenerate {role} ring.")
            rings.append(tuple(ring))
        result[role] = rings
    if not result["outer"]:
        raise ValueError(f"Boundary relation {relation.osm_id} has no outer rings.")
    return result


def _ring_wkt(ring: tuple[int, ...], nodes: dict[int, tuple[float, float]]) -> str:
    pairs = [f"{nodes[node_id][0]:.15g} {nodes[node_id][1]:.15g}" for node_id in ring]
    if pairs[0] != pairs[-1]:
        raise ValueError("Boundary ring does not close at the same coordinate.")
    return "(" + ",".join(pairs) + ")"


def _city_polygon_wkts(relation: Relation, ways: dict[int, Way], nodes: dict[int, tuple[float, float]], db):
    tags = relation.tags
    if tags.get("boundary") != "administrative" or not tags.get("admin_level"):
        raise ValueError(f"Selected relation {relation.osm_id} is not a tagged administrative boundary.")
    rings = _stitch_rings(relation, ways, nodes)
    shells = ["POLYGON(" + _ring_wkt(ring, nodes) + ")" for ring in rings["outer"]]
    holes = ["POLYGON(" + _ring_wkt(ring, nodes) + ")" for ring in rings["inner"]]
    assignments: list[list[str]] = [[] for _ in shells]
    for hole in holes:
        containing = []
        for index, shell in enumerate(shells):
            row = db.execute(text("""
                SELECT ST_Covers(shell.geom, hole_part.geom) AS covered,
                       ST_Area(shell.geom::geography) AS area
                FROM (SELECT ST_GeomFromText(:shell,4326) AS geom) shell,
                     (SELECT ST_GeomFromText(:inner,4326) AS geom) hole_part
            """), {"shell": shell, "inner": hole}).mappings().one()
            if row["covered"]:
                containing.append((row["area"], index))
        if not containing:
            raise ValueError(f"Boundary relation {relation.osm_id} has an inner ring outside all outer rings.")
        assignments[min(containing)[1]].append(hole[8:-1])

    polygons = []
    for shell, assigned_holes in zip(shells, assignments):
        shell_ring = shell[len("POLYGON("):-1]
        polygons.append("POLYGON(" + ",".join([shell_ring, *assigned_holes]) + ")")
    check = db.execute(text("""
        WITH parts AS (
          SELECT ST_GeomFromText(part,4326) AS geom
          FROM unnest(CAST(:polygons AS text[])) AS item(part)
        ), combined AS (SELECT ST_Multi(ST_Collect(geom)) AS geom FROM parts)
        SELECT ST_IsValid(geom) AS valid, ST_IsEmpty(geom) AS empty,
               GeometryType(geom) AS kind, ST_IsValidReason(geom) AS reason
        FROM combined
    """), {"polygons": polygons}).mappings().one()
    if not check["valid"] or check["empty"] or check["kind"] != "MULTIPOLYGON":
        raise ValueError(f"Invalid city boundary relation {relation.osm_id}: {check['reason']}")
    return "MULTIPOLYGON(" + ",".join(p[len("POLYGON"):] for p in polygons) + ")"


def _iso_timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("Source snapshot timestamp is required (pass --source-timestamp).")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Source timestamp must be ISO-8601 with a timezone.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Source timestamp must include a timezone.")
    return result.astimezone(timezone.utc)


def _read_osm_snapshot(path: Path, relation_ids: set[int]) -> tuple[OSMDocument, str]:
    """Hash and parse the same temporary snapshot so import identity cannot drift."""
    digest = hashlib.sha256()
    with path.open("rb") as source, tempfile.TemporaryFile(mode="w+b") as snapshot:
        before = os.fstat(source.fileno())
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            snapshot.write(block)
        after = os.fstat(source.fileno())
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if signature(before) != signature(after):
            raise ValueError("OSM source changed while it was being snapshotted; retry the import.")
        snapshot.seek(0)
        document = _parse_osm_stream(snapshot, relation_ids)
    return document, digest.hexdigest()


def import_osm_xml(
    engine,
    path: str | Path,
    *,
    region: str,
    city_relation_ids: list[int] | tuple[int, ...],
    source_timestamp: str | None = None,
    coverage_mode: str = "sampled",
    coverage_evidence: str | None = None,
) -> dict:
    """Atomically stage a regional XML snapshot, activating only the first version."""
    file_path = Path(path)
    region = region.strip()
    relation_ids = sorted(set(int(value) for value in city_relation_ids))
    if not region or len(region) > 128 or not relation_ids or any(value <= 0 for value in relation_ids):
        raise ValueError("A region and one or more positive city relation IDs are required.")
    if coverage_mode not in {"complete", "sampled"}:
        raise ValueError("coverage_mode must be 'complete' or 'sampled'.")
    if coverage_mode == "complete" and not (coverage_evidence and coverage_evidence.strip()):
        raise ValueError("Complete coverage requires a source/query completeness note.")
    evidence = (coverage_evidence or "Sampled input; completeness not established.").strip()
    if len(evidence) > 2048:
        raise ValueError("coverage_evidence must be at most 2048 characters.")
    document, digest = _read_osm_snapshot(file_path, set(relation_ids))
    snapshot = _iso_timestamp(source_timestamp or document.source_timestamp)
    selection_config = f"{coverage_mode}:{','.join(map(str, relation_ids))}"
    selection_key = hashlib.sha256(selection_config.encode("ascii")).hexdigest()

    with engine.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:region, 11011))"), {"region": region})
        existing = db.execute(text("""
            SELECT id, status, validated_at, coverage_mode FROM map_datasets
            WHERE region=:region AND source_checksum=:checksum
              AND eligibility_rule_version=:rule AND selection_key=:selection
        """), {"region": region, "checksum": digest, "rule": ELIGIBILITY_RULE_VERSION,
              "selection": selection_key}).mappings().first()
        if existing:
            return {"dataset_id": existing["id"], "status": existing["status"],
                    "validated": existing["validated_at"] is not None,
                    "coverage_mode": existing["coverage_mode"], "reused": True}

        dataset_id = db.execute(text("""
            INSERT INTO map_datasets
              (region, source_timestamp, source_checksum, eligibility_rule_version,selection_key,
               coverage_mode,coverage_evidence)
            VALUES (:region,:timestamp,:checksum,:rule,:selection,:coverage_mode,:coverage_evidence)
            RETURNING id
        """), {"region": region, "timestamp": snapshot, "checksum": digest,
              "rule": ELIGIBILITY_RULE_VERSION, "selection": selection_key,
              "coverage_mode": coverage_mode, "coverage_evidence": evidence}).scalar_one()

        for relation_id in relation_ids:
            relation = document.relations[relation_id]
            polygon_wkt = _city_polygon_wkts(relation, document.ways, document.nodes, db)
            city_id = db.execute(text("""
                INSERT INTO cities(dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,:relation,:name,:level,ST_GeomFromText(:boundary,4326))
                RETURNING id
            """), {
                "dataset": dataset_id,
                "relation": relation_id,
                "name": relation.tags.get("name", str(relation_id)),
                "level": relation.tags["admin_level"],
                "boundary": polygon_wkt,
            }).scalar_one()
            _import_city_ways(db, dataset_id, city_id, relation, polygon_wkt, document)
            if coverage_mode == "complete":
                street_count = db.execute(text(
                    "SELECT count(*) FROM streets WHERE dataset_id=:dataset AND city_id=:city"
                ), {"dataset": dataset_id, "city": city_id}).scalar_one()
                if street_count == 0:
                    raise ValueError(f"City relation {relation_id} has no eligible named roads in the extract.")

        for table in ("map_datasets", "cities", "streets", "street_ways", "osm_ways", "street_nodes", "osm_nodes"):
            db.execute(text(f"ANALYZE {table}"))

        db.execute(text("UPDATE map_datasets SET validated_at=now() WHERE id=:id"), {"id": dataset_id})
        has_active = db.execute(text("""
            SELECT 1 FROM map_datasets WHERE region=:region AND status='active'
        """), {"region": region}).first()
        if not has_active and coverage_mode == "complete":
            db.execute(text("UPDATE map_datasets SET status='active' WHERE id=:id"), {"id": dataset_id})
            status = "active"
        else:
            status = "importing"
    return {"dataset_id": dataset_id, "status": status, "validated": True,
            "coverage_mode": coverage_mode, "reused": False}


def _import_city_ways(db, dataset_id: int, city_id: int, relation: Relation, polygon_wkt: str,
                      document: OSMDocument):
    grouped: dict[str, dict] = {}
    bounds = db.execute(text("""
        SELECT ST_XMin(Box2D(boundary)) AS west, ST_YMin(Box2D(boundary)) AS south,
               ST_XMax(Box2D(boundary)) AS east, ST_YMax(Box2D(boundary)) AS north
        FROM cities WHERE id=:city
    """), {"city": city_id}).mappings().one()
    for way in document.ways.values():
        if not eligible_way(way.tags):
            continue
        if len(way.nodes) < 2:
            continue
        missing = [node_id for node_id in way.nodes if node_id not in document.nodes]
        if missing:
            raise ValueError(f"Eligible OSM way {way.osm_id} has missing node references: {missing[:5]}")
        coordinates = [document.nodes[node_id] for node_id in way.nodes]
        if len(set(coordinates)) < 2:
            continue
        if (max(lon for lon, _ in coordinates) < bounds["west"]
                or min(lon for lon, _ in coordinates) > bounds["east"]
                or max(lat for _, lat in coordinates) < bounds["south"]
                or min(lat for _, lat in coordinates) > bounds["north"]):
            continue
        line = "LINESTRING(" + ",".join(f"{lon:.15g} {lat:.15g}" for lon, lat in coordinates) + ")"
        geometry = db.execute(text("""
            WITH city AS (SELECT ST_GeomFromText(:polygon,4326) AS geom),
                 way AS (SELECT ST_GeomFromText(:line,4326) AS geom),
                 refs AS (
                   SELECT node_id,ST_GeomFromText(point_wkt,4326) AS geom
                   FROM unnest(CAST(:node_ids AS bigint[]),CAST(:point_wkts AS text[])) AS p(node_id,point_wkt)
                 ),
                 inside AS (SELECT array_agg(refs.node_id) AS node_ids FROM refs,city
                            WHERE ST_Covers(city.geom,refs.geom))
            SELECT ST_Intersects(city.geom,way.geom) AS intersects,
              COALESCE(inside.node_ids,ARRAY[]::bigint[]) AS inside_node_ids,
              ST_AsText(ST_Multi(way.geom)) AS wkt
            FROM city,way,inside
        """), {
            "polygon": polygon_wkt,
            "line": line,
            "node_ids": list(way.nodes),
            "point_wkts": [f"POINT({lon:.15g} {lat:.15g})" for lon, lat in coordinates],
        }).mappings().one()
        if not geometry["intersects"] or not geometry["inside_node_ids"]:
            continue
        name = way.tags["name"]
        entry = grouped.setdefault(name, {"ways": [], "nodes": set()})
        entry["ways"].append((way, geometry["wkt"]))
        entry["nodes"].update(geometry["inside_node_ids"])

    for name, entry in grouped.items():
        if not entry["nodes"]:
            continue
        street_id = db.execute(text("""
            INSERT INTO streets(dataset_id,city_id,normalized_name,display_name,eligible_node_count)
            VALUES (:dataset,:city,:name,:name,:count) RETURNING id
        """), {"dataset": dataset_id, "city": city_id, "name": name,
              "count": len(entry["nodes"])}).scalar_one()
        for way, line_wkt in entry["ways"]:
            db.execute(text("""
                INSERT INTO osm_ways(dataset_id,osm_way_id,name,tags,geometry)
                VALUES (:dataset,:way,:name,CAST(:tags AS jsonb),ST_GeomFromText(:geometry,4326))
                ON CONFLICT (dataset_id,osm_way_id) DO NOTHING
            """), {"dataset": dataset_id, "way": way.osm_id, "name": way.tags["name"],
                  "tags": json.dumps(way.tags), "geometry": line_wkt})
            db.execute(text("INSERT INTO street_ways(dataset_id,street_id,osm_way_id) VALUES (:dataset,:street,:way)"),
                       {"dataset": dataset_id, "street": street_id, "way": way.osm_id})
        for node_id in sorted(entry["nodes"]):
            lon, lat = document.nodes[node_id]
            db.execute(text("""
                INSERT INTO osm_nodes(dataset_id,osm_node_id,point)
                VALUES (:dataset,:node,ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography)
                ON CONFLICT (dataset_id,osm_node_id) DO NOTHING
            """), {"dataset": dataset_id, "node": node_id, "lon": lon, "lat": lat})
            db.execute(text("INSERT INTO street_nodes(dataset_id,street_id,osm_node_id) VALUES (:dataset,:street,:node)"),
                       {"dataset": dataset_id, "street": street_id, "node": node_id})


def find_cities(engine, dataset_id: int, *, lon: float, lat: float, limit: int = 20) -> list[dict]:
    """Return at most 20 cities whose indexed boundaries cover a WGS84 point."""
    _validate_point(lon, lat)
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    with engine.connect() as db:
        rows = db.execute(text("""
            SELECT c.id,c.dataset_id,c.osm_relation_id,c.name,c.admin_level,
              ST_XMin(Box2D(c.boundary)) AS west, ST_YMin(Box2D(c.boundary)) AS south,
              ST_XMax(Box2D(c.boundary)) AS east, ST_YMax(Box2D(c.boundary)) AS north
            FROM cities c
            WHERE c.dataset_id=:dataset AND c.boundary && ST_SetSRID(ST_MakePoint(:lon,:lat),4326)
              AND ST_Covers(c.boundary,ST_SetSRID(ST_MakePoint(:lon,:lat),4326))
            ORDER BY c.id LIMIT :limit
        """), {"dataset": int(dataset_id), "lon": lon, "lat": lat, "limit": limit}).mappings().all()
    return [dict(row) for row in rows]


def find_nearby_nodes(engine, dataset_id: int, *, lon: float, lat: float,
                      radius_m: float = DEFAULT_NEARBY_RADIUS_M, limit: int = 100) -> list[dict]:
    """Return bounded nearest original OSM nodes using the geography GiST index."""
    _validate_point(lon, lat)
    if not math.isfinite(radius_m) or not 0 < radius_m <= MAX_NEARBY_RADIUS_M:
        raise ValueError(f"radius_m must be in (0,{MAX_NEARBY_RADIUS_M:g}]")
    if not 1 <= limit <= MAX_LOOKUP_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_LOOKUP_ROWS}")
    with engine.connect() as db:
        rows = db.execute(text("""
            WITH target AS (SELECT ST_SetSRID(ST_MakePoint(:lon,:lat),4326)::geography AS point)
            SELECT n.dataset_id,n.osm_node_id,ST_X(n.point::geometry) AS lon,
              ST_Y(n.point::geometry) AS lat,ST_Distance(n.point,target.point) AS distance_m
            FROM osm_nodes n,target
            WHERE n.dataset_id=:dataset AND ST_DWithin(n.point,target.point,:radius)
            ORDER BY n.point <-> target.point,n.osm_node_id LIMIT :limit
        """), {"dataset": int(dataset_id), "lon": lon, "lat": lat,
              "radius": radius_m, "limit": limit}).mappings().all()
    return [dict(row) for row in rows]


def _validate_point(lon: float, lat: float):
    if (not math.isfinite(lon) or not math.isfinite(lat)
            or not -180 <= lon <= 180 or not -90 <= lat <= 90):
        raise ValueError("Expected finite WGS84 coordinates [longitude, latitude].")


def _cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Regional .osm XML extract")
    parser.add_argument("--region", required=True, help="Stable region key, e.g. be/gent")
    parser.add_argument("--city-relation", required=True, type=int, action="append",
                        help="Explicit OSM administrative city relation ID; repeatable")
    parser.add_argument("--source-timestamp", help="Override embedded timestamp; ISO-8601 with timezone")
    parser.add_argument("--coverage-mode", choices=("complete", "sampled"), default="sampled",
                        help="Only complete inputs can activate; sampled inputs remain staged")
    parser.add_argument("--coverage-evidence", help="Source/query note required for complete coverage")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    result = import_osm_xml(create_engine(database_url, pool_pre_ping=True), args.file,
                             region=args.region, city_relation_ids=args.city_relation,
                             source_timestamp=args.source_timestamp, coverage_mode=args.coverage_mode,
                             coverage_evidence=args.coverage_evidence)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _cli()
