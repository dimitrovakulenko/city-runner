import json
import os
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def create_app(engine: Engine | None = None, identity_resolver: Callable[..., Any] | None = None) -> FastAPI:
    engine = engine or create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg://localhost/activities"), pool_pre_ping=True)
    app = FastAPI()
    app.state.engine = engine

    def current_user() -> str:
        if identity_resolver is None:
            raise HTTPException(status_code=401, detail="Authentication is not configured.")
        identity = identity_resolver()
        if not identity:
            raise HTTPException(status_code=401, detail="Unauthenticated.")
        return str(identity)

    @app.get("/api/activities")
    def activities(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        q: str | None = Query(None, max_length=200),
        user_id: str = Depends(current_user),
    ):
        filters = "user_id = :user_id"
        params: dict[str, Any] = {
            "user_id": user_id,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        if q:
            filters += " AND (lower(name) LIKE lower(:q) OR lower(coalesce(date, '')) LIKE lower(:q) " \
                       "OR lower(coalesce(activity_type, '')) LIKE lower(:q))"
            params["q"] = f"%{q}%"
        with engine.connect() as db:
            total = db.execute(text(f"SELECT count(*) FROM activities WHERE {filters}"), params).scalar_one()
            rows = db.execute(text(f"""SELECT id, name, date, activity_type, processed, unmapped_points FROM activities
                WHERE {filters} ORDER BY CASE WHEN date IS NULL OR date='' OR date='unknown' THEN 1 ELSE 0 END,
                date DESC, id DESC LIMIT :limit OFFSET :offset"""), params).mappings().all()
        items = [{
            "id": str(r["id"]),
            "name": r["name"],
            "date": r["date"] or "unknown",
            "type": r["activity_type"] or "unknown",
            "processed": bool(r["processed"]),
            "unmapped_points": r["unmapped_points"],
        } for r in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": total}

    @app.get("/api/activities/{activity_id}")
    def activity(activity_id: int, user_id: str = Depends(current_user)):
        with engine.connect() as db:
            row = db.execute(text("""SELECT id, name, date, activity_type, tracks, timestamps,
                processed, unmapped_points
                FROM activities WHERE id=:id AND user_id=:user_id"""),
                {"id": activity_id, "user_id": user_id}).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Activity not found.")
        tracks = row["tracks"] or []
        timestamps = row["timestamps"] or []
        if isinstance(tracks, str):
            tracks = json.loads(tracks)
        if isinstance(timestamps, str):
            timestamps = json.loads(timestamps)
        points = [point for segment in tracks for point in segment]
        bounds = [[min(p[0] for p in points), min(p[1] for p in points)],
                  [max(p[0] for p in points), max(p[1] for p in points)]] if points else None
        return {"id": str(row["id"]), "name": row["name"], "date": row["date"] or "unknown",
                "type": row["activity_type"] or "unknown", "processed": bool(row["processed"]),
                "unmapped_points": row["unmapped_points"], "tracks": tracks,
                "timestamps": timestamps, "bounds": bounds}

    return app


app = create_app()
