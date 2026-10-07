"""Canonical activity-selection filters shared by activity, map and progress APIs."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine


SourceFilter = Literal["all", "gpx", "fit", "unknown"]
_ISO_DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class ActivityFilters(BaseModel):
    date_from: str | None = None
    date_to: str | None = None
    activity_type: str | None = None
    source: SourceFilter = "all"


class ActivityFilterOptions(BaseModel):
    activity_types: list[str]
    types_truncated: bool


def create_activity_filters_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/activities/filters", response_model=ActivityFilterOptions)
    def activity_filter_options(account_id: str = Depends(current_user)):
        with engine.connect() as db:
            rows = db.execute(text("""SELECT DISTINCT lower(trim(activity_type)) AS activity_type
                FROM activities WHERE user_id=:account AND trim(coalesce(activity_type,''))<>''
                  AND lower(trim(activity_type))<>'unknown'
                ORDER BY activity_type LIMIT 101"""), {"account": account_id}).scalars().all()
        return ActivityFilterOptions(activity_types=list(rows[:100]), types_truncated=len(rows) > 100)

    return router


def canonical_filters(date_from: str | None = None, date_to: str | None = None,
                     activity_type: str | None = None, source: SourceFilter = "all") -> ActivityFilters:
    dates = []
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is None or value == "":
            dates.append(None)
            continue
        if not _ISO_DATE.fullmatch(value):
            raise HTTPException(status_code=422, detail=f"{name} must be an ISO calendar date.")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"{name} must be an ISO calendar date.") from error
        dates.append(value)
    if dates[0] and dates[1] and dates[0] > dates[1]:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to.")
    canonical_type = activity_type.strip().lower() if activity_type else None
    canonical_type = canonical_type or None
    if canonical_type is not None and len(canonical_type) > 80:
        raise HTTPException(status_code=422, detail="activity_type must be at most 80 characters.")
    return ActivityFilters(date_from=dates[0], date_to=dates[1], activity_type=canonical_type, source=source)


def activity_predicate(filters: ActivityFilters, alias: str, params: dict, *,
                       account_param: str = "account_id", include_source: bool = True,
                       dialect: str = "postgresql") -> str:
    """Return SQL for a trusted activity alias and bind canonical values into params."""
    clauses: list[str] = []
    if filters.date_from or filters.date_to:
        if dialect == "sqlite":
            valid_date = (f"substr({alias}.date,1,4)<>'0000' AND "
                          f"strftime('%Y-%m-%d',{alias}.date,'+0 days')={alias}.date")
        else:
            valid_date = f"CASE WHEN {alias}.date ~ '^[0-9]{{4}}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$' THEN " \
                f"CASE WHEN substring({alias}.date from 1 for 4)='0000' THEN FALSE " \
                f"WHEN substring({alias}.date from 6 for 2)='02' THEN " \
                f"substring({alias}.date from 9 for 2) <= CASE WHEN " \
                f"(substring({alias}.date from 1 for 4)::integer % 400=0 OR " \
                f"(substring({alias}.date from 1 for 4)::integer % 4=0 AND " \
                f"substring({alias}.date from 1 for 4)::integer % 100<>0)) THEN '29' ELSE '28' END " \
                f"WHEN substring({alias}.date from 6 for 2) IN ('04','06','09','11') THEN " \
                f"substring({alias}.date from 9 for 2)<='30' ELSE TRUE END ELSE FALSE END"
        clauses.append(valid_date)
        if filters.date_from:
            clauses.append(f"{alias}.date >= :activity_date_from")
            params["activity_date_from"] = filters.date_from
        if filters.date_to:
            clauses.append(f"{alias}.date <= :activity_date_to")
            params["activity_date_to"] = filters.date_to
    if filters.activity_type:
        if filters.activity_type == "unknown":
            clauses.append(f"lower(trim(coalesce({alias}.activity_type,''))) IN ('','unknown')")
        else:
            clauses.append(f"lower(trim(coalesce({alias}.activity_type,'')))=:activity_type")
            params["activity_type"] = filters.activity_type
    if include_source:
        if filters.source == "unknown":
            clauses.append(f"NOT EXISTS (SELECT 1 FROM activity_sources afs WHERE afs.account_id=:{account_param} AND afs.activity_id={alias}.id)")
        elif filters.source in {"gpx", "fit"}:
            clauses.append(f"EXISTS (SELECT 1 FROM activity_sources afs WHERE afs.account_id=:{account_param} "
                           f"AND afs.activity_id={alias}.id AND afs.source_kind=:activity_source)")
            params["activity_source"] = filters.source
    return " AND ".join(clauses) if clauses else "TRUE"
