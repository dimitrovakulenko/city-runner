"""Local Garmin PoC storage, OSM discovery, and node coverage."""

import gzip
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

import requests
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import polygonize, unary_union


DATA = Path(os.environ.get("POC_DATA_DIR", Path(__file__).resolve().parents[1] / ".poc-data"))


class Store:
    def __init__(self, directory=DATA):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "progress.sqlite"
        self.polygons = {}
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cities (id INTEGER PRIMARY KEY, name TEXT, geometry TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS city_bounds USING rtree(id,minx,maxx,miny,maxy);
                CREATE TABLE IF NOT EXISTS streets (id TEXT PRIMARY KEY, city_id INTEGER, name TEXT, geometry TEXT);
                CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, lon REAL, lat REAL);
                CREATE VIRTUAL TABLE IF NOT EXISTS node_bounds USING rtree(id,minx,maxx,miny,maxy);
                CREATE TABLE IF NOT EXISTS street_nodes (street_id TEXT, node_id INTEGER, PRIMARY KEY(street_id,node_id));
                CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY, name TEXT, date TEXT, tracks TEXT, processed INTEGER DEFAULT 0, unmapped INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS hits (activity_id INTEGER, node_id INTEGER, PRIMARY KEY(activity_id,node_id));
                CREATE INDEX IF NOT EXISTS hits_node ON hits(node_id);
                CREATE TABLE IF NOT EXISTS unresolved (lon REAL, lat REAL, PRIMARY KEY(lon,lat));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def add_activity(self, activity_id, name, date, gpx):
        tracks = parse_gpx(gpx)
        if gpx:
            folder = self.directory / "tracks"
            folder.mkdir(exist_ok=True, mode=0o700)
            path = folder / f"{int(activity_id)}.gpx.gz"
            with gzip.open(path, "wb") as handle:
                handle.write(gpx)
            path.chmod(0o600)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO activities(id,name,date,tracks) VALUES (?,?,?,?)",
                       (int(activity_id), name, date, json.dumps(tracks)))

    def has_activity(self, activity_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM activities WHERE id=?", (activity_id,)).fetchone() is not None

    def known_city(self, lon, lat):
        with self.db() as db:
            rows = db.execute("""SELECT c.* FROM cities c JOIN city_bounds b ON b.id=c.id
                WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?""", (lon, lon, lat, lat)).fetchall()
        for row in rows:
            if row["id"] not in self.polygons:
                self.polygons[row["id"]] = shape(json.loads(row["geometry"]))
            polygon = self.polygons[row["id"]]
            if polygon.covers(Point(lon, lat)):
                return row["id"]
        return None

    def add_city(self, relation, road_data):
        polygon = relation_polygon(relation)
        city_id = relation["id"]
        osm_nodes = {node["id"]: node for node in road_data["elements"] if node["type"] == "node"}
        streets = {}
        for way in road_data["elements"]:
            if way["type"] != "way" or not way.get("tags", {}).get("name"):
                continue
            tags = way["tags"]
            if (not tags.get("highway") or tags.get("access") in {"private", "no"} or tags.get("foot") == "no"
                    or tags["highway"] in {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed", "raceway"}):
                continue
            points = [osm_nodes[node_id] for node_id in way["nodes"] if node_id in osm_nodes]
            if len(points) < 2:
                continue
            line = LineString([(point["lon"], point["lat"]) for point in points]).intersection(polygon)
            inside = [point for point in points if polygon.covers(Point(point["lon"], point["lat"]))]
            if line.is_empty or not inside:
                continue
            name = way["tags"]["name"]
            entry = streets.setdefault(name, {"lines": [], "nodes": {}})
            entry["lines"].append(line)
            entry["nodes"].update({point["id"]: point for point in inside})
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO cities VALUES (?,?,?)",
                       (city_id, relation["tags"].get("name", str(city_id)), json.dumps(mapping(polygon))))
            minx, miny, maxx, maxy = polygon.bounds
            db.execute("INSERT OR REPLACE INTO city_bounds VALUES (?,?,?,?,?)", (city_id, minx, maxx, miny, maxy))
            for name, street in streets.items():
                street_id = f"{city_id}:{name}"
                db.execute("INSERT OR REPLACE INTO streets VALUES (?,?,?,?)",
                           (street_id, city_id, name, json.dumps(mapping(unary_union(street["lines"])))))
                for point in street["nodes"].values():
                    db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?)", (point["id"], point["lon"], point["lat"]))
                    db.execute("INSERT OR IGNORE INTO node_bounds VALUES (?,?,?,?,?)",
                               (point["id"], point["lon"], point["lon"], point["lat"], point["lat"]))
                    db.execute("INSERT OR IGNORE INTO street_nodes VALUES (?,?)", (street_id, point["id"]))
            # Earlier tracks may also visit newly discovered nodes near a city border.
            db.execute("UPDATE activities SET processed=0")
        self.polygons[city_id] = polygon

    def process(self, activity_id, osm):
        with self.db() as db:
            row = db.execute("SELECT tracks FROM activities WHERE id=?", (activity_id,)).fetchone()
        points = [point for segment in json.loads(row[0]) for point in segment]
        unmapped = 0
        for lon, lat in points:
            if self.known_city(lon, lat) is not None:
                continue
            # Cache exact unresolved coordinates, not a tile that could hide a border crossing.
            with self.db() as db:
                missing = db.execute("SELECT 1 FROM unresolved WHERE lon=? AND lat=?", (lon, lat)).fetchone()
            if missing:
                unmapped += 1
                continue
            relation = osm.city(lon, lat)
            if relation is None:
                with self.db() as db:
                    db.execute("INSERT OR IGNORE INTO unresolved VALUES (?,?)", (lon, lat))
                unmapped += 1
            else:
                self.add_city(relation, osm.roads(relation["id"]))
        matched = set()
        with self.db() as db:
            for lon, lat in points:
                dy = 25 / 111000
                dx = dy / max(0.001, math.cos(math.radians(lat)))
                candidates = db.execute("""SELECT n.* FROM node_bounds b JOIN nodes n ON n.id=b.id
                    WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?""",
                    (lon + dx, lon - dx, lat + dy, lat - dy))
                matched.update(node["id"] for node in candidates if distance(lon, lat, node["lon"], node["lat"]) <= 25)
            db.execute("DELETE FROM hits WHERE activity_id=?", (activity_id,))
            db.executemany("INSERT INTO hits VALUES (?,?)", [(activity_id, node_id) for node_id in matched])
            db.execute("UPDATE activities SET processed=1, unmapped=? WHERE id=?", (unmapped, activity_id))

    def streets(self):
        with self.db() as db:
            rows = db.execute("""SELECT s.*, COUNT(sn.node_id) AS total,
                SUM(EXISTS(SELECT 1 FROM hits h WHERE h.node_id=sn.node_id)) AS visited
                FROM streets s JOIN street_nodes sn ON sn.street_id=s.id GROUP BY s.id""").fetchall()
        return [dict(row) | {"complete": row["visited"] >= (row["total"] if row["total"] < 10 else math.ceil(row["total"] * 0.9))}
                for row in rows]

    def cities(self):
        streets = self.streets()
        with self.db() as db:
            rows = db.execute("SELECT c.*, b.minx,b.maxx,b.miny,b.maxy FROM cities c JOIN city_bounds b ON b.id=c.id ORDER BY c.name").fetchall()
        result = []
        for row in rows:
            entries = [street for street in streets if street["city_id"] == row["id"]]
            completed = sum(street["complete"] for street in entries)
            result.append({"id": row["id"], "name": row["name"], "total": len(entries), "completed": completed,
                           "percent": round(100 * completed / len(entries), 1) if entries else 0,
                           "bounds": [[row["minx"], row["miny"]], [row["maxx"], row["maxy"]]]})
        return result


def parse_gpx(content):
    if not content:
        return []
    root = ElementTree.fromstring(content)
    if root.tag.split("}")[-1] != "gpx":
        raise ValueError("Expected a GPX document.")
    segments = root.findall(".//{*}trkseg") or [root]
    tracks = []
    for segment in segments:
        points = []
        for point in segment.findall(".//{*}trkpt"):
            lon, lat = float(point.attrib["lon"]), float(point.attrib["lat"])
            if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                points.append([lon, lat])
        if points:
            tracks.append(points)
    return tracks


def distance(lon1, lat1, lon2, lat2):
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, a)))


def relation_polygon(relation):
    outer, inner = [], []
    for member in relation.get("members", []):
        points = [(point["lon"], point["lat"]) for point in member.get("geometry", [])]
        if member["type"] == "way" and len(points) >= 2:
            (inner if member.get("role") == "inner" else outer).append(LineString(points))
    polygon = unary_union(list(polygonize(outer))).difference(unary_union(list(polygonize(inner))))
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("OSM city boundary is missing or invalid; retry or inspect its relation.")
    return polygon


class Overpass:
    def __init__(self, allow_location_lookup=True):
        self.allow_location_lookup = allow_location_lookup
        self.endpoint = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
        self.last_request = 0
        levels = os.environ.get("POC_ADMIN_LEVELS", "8").split(",")
        if not all(level.isdigit() and 1 <= int(level) <= 12 for level in levels):
            raise ValueError("POC_ADMIN_LEVELS must be comma-separated OSM admin levels.")
        self.levels = levels

    def query(self, query, method="POST"):
        time.sleep(max(0, 2 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        payload = {"data": f"[out:json][timeout:90];{query}"}
        response = requests.request(method, self.endpoint, **({"params": payload} if method == "GET" else {"data": payload}),
                                    headers={"User-Agent": "CityRunnerPersonalPoC/0.1"}, timeout=120)
        response.raise_for_status()
        data = response.json()
        if data.get("remark"):
            raise RuntimeError("Overpass returned an incomplete query; retry after the server recovers.")
        return data

    def city(self, lon, lat):
        if not self.allow_location_lookup:
            return None
        levels = "|".join(self.levels)
        data = self.query(f'is_in({lat},{lon});area._["boundary"="administrative"]["admin_level"~"^({levels})$"];rel(pivot);out body geom;')
        candidates = [relation for relation in data["elements"] if relation["type"] == "relation"]
        return min(candidates, key=lambda relation: (self.levels.index(relation["tags"]["admin_level"]), relation["id"])) if candidates else None

    def roads(self, relation_id):
        data = self.query(f'area({3600000000 + relation_id})->.city;way(area.city)["highway"]["name"];out body geom;', method="GET")
        return geometry_roads(data)


def geometry_roads(data):
    """Normalize Overpass way geometry to shared existing OSM node IDs."""
    nodes = {}
    for way in data["elements"]:
        if way["type"] != "way":
            continue
        geometry = way.get("geometry", [])
        if len(geometry) != len(way["nodes"]) or any(not point or "lat" not in point or "lon" not in point for point in geometry):
            raise ValueError("OSM returned incomplete road geometry; retry the city download.")
        nodes.update({node_id: {"type": "node", "id": node_id, **point} for node_id, point in zip(way["nodes"], geometry)})
    return {"elements": data["elements"] + list(nodes.values())}
