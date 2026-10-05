"""One local user, one browser map, and a five-minute Garmin poll."""

import asyncio
import hashlib
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from xml.etree import ElementTree

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from shapely.geometry import LineString, box, mapping, shape

from poc.core import Overpass, Store, parse_gpx
from poc.garmin_sync import sync


def create_app(store=None, password=None, poll=True):
    store = store or Store()
    password = password or os.environ.get("POC_PASSWORD")
    password_file = store.directory / "app-password"
    if not password:
        if not password_file.exists():
            with open(password_file, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
                file.write(secrets.token_urlsafe(24))
        password = password_file.read_text().strip()
    security = HTTPBasic()
    lock = threading.Lock()

    def authorize(credentials: HTTPBasicCredentials = Depends(security)):
        valid_user = secrets.compare_digest(credentials.username.encode(), b"poc")
        valid_password = secrets.compare_digest(credentials.password.encode(), password.encode())
        if not (valid_user and valid_password):
            raise HTTPException(401, "Sign in as poc with your local PoC password.", headers={"WWW-Authenticate": "Basic"})

    def start_sync():
        if not lock.acquire(blocking=False):
            return False
        store.set("sync", {"running": True, "stage": "Connecting to Garmin"})

        def run():
            try:
                sync(store)
            except Exception as error:
                store.set("sync", {"running": False, "stage": "Sync failed; check Garmin login and connectivity",
                                   "error": type(error).__name__})
            finally:
                lock.release()

        threading.Thread(target=run, daemon=True).start()
        return True

    async def polling():
        while True:
            if (store.directory / "garmin" / "garmin_tokens.json").exists():
                start_sync()
            await asyncio.sleep(300)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(polling()) if poll else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def private_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/api/status")
    def status():
        with store.db() as db:
            counts = dict(db.execute("""SELECT COUNT(*) AS activities,
                SUM(processed) AS processed, SUM(unmapped) AS unmapped_points,
                SUM(tracks='[]') AS without_foot_gps, SUM(id<0) AS file_imports FROM activities""").fetchone())
        return {"profile": store.get("profile"), "demo": store.get("demo", False), "location_lookup_pending": store.get("location_lookup_pending", False), "sync": store.get("sync", {"running": False, "stage": "Connect Garmin using the local login command"}),
                "counts": counts, "import_error": store.get("import_error"), "processing_error": store.get("processing_error"),
                "history_complete": store.get("history_complete", False)}

    @app.post("/api/sync")
    def trigger(request: Request):
        # Require a same-origin script header so HTTP Basic credentials cannot authorize a cross-site form POST.
        if request.headers.get("x-poc-request") != "1":
            raise HTTPException(403, "Use the PoC sync button.")
        if store.get("demo"):
            raise HTTPException(409, "This is synthetic demo data. Use the normal PoC for Garmin sync.")
        return {"started": start_sync()}

    @app.get("/api/cities")
    def cities():
        return store.cities()

    @app.post("/api/import")
    async def import_gpx(request: Request, name: str = Query("Imported GPX", min_length=1, max_length=200)):
        if request.headers.get("x-poc-request") != "1":
            raise HTTPException(403, "Use the GPX import button.")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 10 * 1024 * 1024:
                raise HTTPException(413, "GPX files must be smaller than 10 MB.")
        content = bytes(content)
        try:
            tracks = parse_gpx(content)
            if not tracks:
                raise ValueError()
        except (ElementTree.ParseError, ValueError, KeyError):
            raise HTTPException(400, "Choose a valid GPX file containing GPS track points.") from None
        activity_id = -(int.from_bytes(hashlib.sha256(content).digest()[:8], "big") & ((1 << 63) - 1) or 1)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "Another import or Garmin sync is running. Retry when it finishes.")
        duplicate = store.has_activity(activity_id)
        store.set("sync", {"running": True, "stage": f"Importing {name}"})

        try:
            if not duplicate:
                await asyncio.to_thread(store.add_activity, activity_id, name, "", content)
        except Exception:
            store.set("sync", {"running": False, "stage": "GPX could not be saved"})
            lock.release()
            raise HTTPException(500, "GPX could not be saved. Check local storage.") from None

        def process_file():
            try:
                osm = Overpass(allow_location_lookup=not store.get("location_lookup_pending", False))
                while True:
                    with store.db() as db:
                        pending = db.execute("SELECT id FROM activities WHERE processed=0").fetchall()
                    if not pending:
                        break
                    for row in pending:
                        store.process(row["id"], osm)
                store.set("processing_error", None)
                stage = f"{'Already imported' if duplicate else 'Imported'}: {name}"
                if store.get("location_lookup_pending"):
                    stage += " · cached streets only; further location lookup needs consent"
                store.set("sync", {"running": False, "stage": stage})
            except Exception as error:
                store.set("processing_error", {"activity_id": activity_id, "kind": type(error).__name__})
                store.set("sync", {"running": False, "stage": "GPX saved; matching failed. Import again to retry.", "error": type(error).__name__})
            finally:
                lock.release()

        threading.Thread(target=process_file, daemon=True).start()
        points = [point for segment in tracks for point in segment]
        return {"duplicate": duplicate, "points": len(points),
                "bounds": [[min(p[0] for p in points), min(p[1] for p in points)],
                           [max(p[0] for p in points), max(p[1] for p in points)]]}

    @app.get("/api/map")
    def map_data(bbox: str = "-180,-85,180,85", zoom: float = Query(10, ge=0, le=24)):
        try:
            values = list(map(float, bbox.split(",")))
            if len(values) != 4 or not (-180 <= values[0] < values[2] <= 180 and -90 <= values[1] < values[3] <= 90):
                raise ValueError()
            viewport = box(*values)
        except ValueError:
            raise HTTPException(400, "Invalid viewport bounds.") from None
        features, nodes = [], []
        for street in store.streets():
            geometry = shape(json.loads(street["geometry"]))
            if geometry.intersects(viewport):
                features.append({"type": "Feature", "geometry": json.loads(street["geometry"]), "properties": {
                    "name": street["name"], "kind": "street", "state": "complete" if street["complete"] else "partial" if street["visited"] else "missing",
                    "visited": street["visited"], "total": street["total"], "city_id": street["city_id"]}})
        with store.db() as db:
            for row in db.execute("SELECT id,name,tracks FROM activities WHERE tracks!='[]'"):
                for segment in json.loads(row["tracks"]):
                    if len(segment) < 2:
                        continue
                    line = LineString(segment)
                    if line.intersects(viewport):
                        tolerance = max(0.000002, 0.04 / (2 ** max(0, zoom - 5)))
                        features.append({"type": "Feature", "geometry": mapping(line.simplify(tolerance)),
                                         "properties": {"name": row["name"], "kind": "track", "activity_id": row["id"]}})
            if zoom >= 14:
                minx, miny, maxx, maxy = values
                rows = db.execute("""SELECT n.*, EXISTS(SELECT 1 FROM hits h WHERE h.node_id=n.id) AS visited
                    FROM nodes n JOIN node_bounds b ON b.id=n.id
                    WHERE b.minx>=? AND b.maxx<=? AND b.miny>=? AND b.maxy<=? LIMIT 20001""", (minx, maxx, miny, maxy)).fetchall()
                nodes = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
                          "properties": {"visited": bool(row["visited"])}} for row in rows[:20000]]
        return {"features": {"type": "FeatureCollection", "features": features},
                "nodes": {"type": "FeatureCollection", "features": nodes}, "nodes_truncated": len(rows) > 20000 if zoom >= 14 else False}

    return app


app = create_app()
