"""Authenticated activity listing and detail routes."""

import json

from fastapi import APIRouter, HTTPException, Query


def _activity(row, detail=False):
    tracks = json.loads(row["tracks"] or "[]")
    points = [point for segment in tracks for point in segment]
    result = {"id": str(row["id"]), "name": row["name"], "date": row["date"] or "unknown",
              "type": row["activity_type"] or "unknown"}
    if detail:
        result.update(tracks=tracks, timestamps=json.loads(row["timestamps"] or "[]"),
                      bounds=[[min(p[0] for p in points), min(p[1] for p in points)],
                              [max(p[0] for p in points), max(p[1] for p in points)]] if points else None)
    return result


def create_router(store):
    router = APIRouter()

    @router.get("/api/activities")
    def activities(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
                   name: str | None = Query(None, max_length=200)):
        where = "WHERE name LIKE ? COLLATE NOCASE" if name else ""
        args = (f"%{name}%",) if name else ()
        with store.db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM activities {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT id,name,date,activity_type FROM activities {where} "
                              "ORDER BY CASE WHEN date IS NULL OR date='' OR date='unknown' THEN 1 ELSE 0 END, "
                              "date DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [_activity(row) for row in rows], "limit": limit, "offset": offset, "total": total}

    @router.get("/api/activities/{activity_id}")
    def activity(activity_id: int):
        with store.db() as db:
            row = db.execute("SELECT id,name,date,activity_type,tracks,timestamps FROM activities WHERE id=?",
                             (activity_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Activity not found.")
        return _activity(row, detail=True)

    return router
