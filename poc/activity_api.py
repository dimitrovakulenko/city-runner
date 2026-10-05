"""Authenticated activity listing and detail routes."""

import json

from fastapi import APIRouter, HTTPException, Query


def _activity(row, detail=False):
    result = {"id": str(row["id"]), "name": row["name"], "date": row["date"] or "unknown",
              "type": row["activity_type"] or "unknown", "processed": bool(row["processed"]),
              "unmapped_points": row["unmapped"]}
    if detail:
        tracks = json.loads(row["tracks"] or "[]")
        points = [point for segment in tracks for point in segment]
        result.update(tracks=tracks, timestamps=json.loads(row["timestamps"] or "[]"),
                      bounds=[[min(p[0] for p in points), min(p[1] for p in points)],
                              [max(p[0] for p in points), max(p[1] for p in points)]] if points else None)
    return result


def create_router(store):
    router = APIRouter()

    @router.get("/api/activities")
    def activities(q: str = Query("", max_length=200), page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
        where = "" if not q else "WHERE name LIKE ? COLLATE NOCASE OR date LIKE ? COLLATE NOCASE OR activity_type LIKE ? COLLATE NOCASE"
        args = (f"%{q}%",) * 3 if q else ()
        offset = (page - 1) * page_size
        with store.db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM activities {where}", args).fetchone()[0]
            rows = db.execute(f"SELECT id,name,date,activity_type,processed,unmapped FROM activities {where} "
                              "ORDER BY CASE WHEN date IS NULL OR date='' OR date='unknown' THEN 1 ELSE 0 END, "
                              "date DESC, id DESC LIMIT ? OFFSET ?", (*args, page_size, offset)).fetchall()
        return {"items": [_activity(row) for row in rows], "page": page, "page_size": page_size, "total": total}

    @router.get("/api/activities/{activity_id}")
    def activity(activity_id: int):
        with store.db() as db:
            row = db.execute("SELECT id,name,date,activity_type,processed,unmapped,tracks,timestamps FROM activities WHERE id=?",
                             (activity_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Activity not found.")
        return _activity(row, detail=True)

    return router
