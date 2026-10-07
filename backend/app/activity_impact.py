"""Owner-scoped historical impact for a successfully matched activity."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.app.explorer import (
    DEFAULT_PAGE_SIZE,
    MAX_BIGINT,
    MAX_PAGE_SIZE,
    DatasetCoverage,
    Rule,
    _MATCHED_NODES_SQL,
    _coverage,
    _dataset,
    _snapshot,
)


MAX_HISTORY_ACTIVITIES = 10_000
MAX_PAGE = 1_000_000
HistoryStatus = Literal["ready", "unknown-dates", "history-limit", "missing-provenance"]


class ActivityImpactStreet(BaseModel):
    street_id: str
    city_id: str
    dataset_id: str
    name: str
    city_name: str
    eligible_nodes: int
    supported_nodes: int
    new_nodes: int | None
    before_nodes: int | None
    after_nodes: int | None
    completed_by_activity: bool | None
    current_nodes: int | None
    bounds: list[float]


class ActivityImpactPage(BaseModel):
    activity_id: str
    dataset_id: str
    dataset_state: Literal["active", "importing", "retired"]
    rule: Rule
    coverage: DatasetCoverage
    history_status: HistoryStatus
    supported_nodes: int | None
    new_nodes: int | None
    streets_advanced: int | None
    streets_completed: int | None
    streets: list[ActivityImpactStreet]
    page: int
    page_size: int
    total: int | None


def create_activity_impact_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/activities/{activity_id}/impact", response_model=ActivityImpactPage)
    def activity_impact(
        activity_id: int = Path(ge=-9_223_372_036_854_775_808, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        rule: Rule = Query("normal"),
        page: int = Query(1, ge=1, le=MAX_PAGE),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        account_id: str = Depends(current_user),
    ):
        with _snapshot(engine) as db:
            activity = db.execute(text("""SELECT id,date FROM activities
                WHERE id=:activity AND user_id=:account"""), {
                "activity": activity_id, "account": account_id,
            }).mappings().first()
            if activity is None:
                raise HTTPException(status_code=404, detail="Activity not found.")

            dataset = _dataset(db, dataset_id)
            coverage = _coverage(db, account_id, dataset_id, dataset)
            has_provenance = db.execute(text(_TARGET_PROVENANCE_QUERY), {
                "account_id": account_id, "activity_id": activity_id, "dataset_id": dataset_id,
            }).scalar_one_or_none() is not None
            if not has_provenance:
                return ActivityImpactPage(
                    activity_id=str(activity_id), dataset_id=str(dataset_id),
                    dataset_state=dataset["state"], rule=rule, coverage=coverage,
                    history_status="missing-provenance", supported_nodes=None, new_nodes=None,
                    streets_advanced=None, streets_completed=None, streets=[],
                    page=page, page_size=page_size, total=None,
                )

            history_rows = db.execute(text(_HISTORY_ACTIVITIES_QUERY), {
                "account_id": account_id, "dataset_id": dataset_id,
                "activity_id": activity_id, "history_limit": MAX_HISTORY_ACTIVITIES + 1,
            }).mappings().all()
            if len(history_rows) > MAX_HISTORY_ACTIVITIES:
                history_status: HistoryStatus = "history-limit"
                before_ids: list[int] = []
            else:
                parsed: list[tuple[int, date]] = []
                dates_valid = True
                for row in history_rows:
                    parsed_date = _calendar_date(row["date"])
                    if parsed_date is None:
                        dates_valid = False
                        break
                    parsed.append((int(row["id"]), parsed_date))
                if not dates_valid:
                    history_status = "unknown-dates"
                    before_ids = []
                else:
                    history_status = "ready"
                    target_date = next(d for identifier, d in parsed if identifier == activity_id)
                    before_ids = [identifier for identifier, item_date in parsed
                                  if (item_date, identifier) < (target_date, activity_id)]

            if coverage.status != "ready":
                return ActivityImpactPage(
                    activity_id=str(activity_id), dataset_id=str(dataset_id),
                    dataset_state=dataset["state"], rule=rule, coverage=coverage,
                    history_status=history_status, supported_nodes=None, new_nodes=None,
                    streets_advanced=None, streets_completed=None, streets=[],
                    page=page, page_size=page_size, total=None,
                )

            rows = db.execute(text(_IMPACT_PAGE_QUERY.replace("{{matched_nodes}}", _MATCHED_NODES_SQL)), {
                "account_id": account_id, "activity_id": activity_id, "dataset_id": dataset_id,
                "before_ids": before_ids, "rule": rule,
                "limit": page_size, "offset": (page - 1) * page_size,
                "history_ready": history_status == "ready",
            }).mappings().all()
            first = rows[0]
            summary_supported = int(first["supported_nodes"])
            total = int(first["total"])
            if history_status == "ready":
                summary_new = int(first["new_nodes"])
                summary_advanced = int(first["streets_advanced"])
                summary_completed = int(first["streets_completed"])
            else:
                summary_new = summary_advanced = summary_completed = None

            streets = []
            for row in rows:
                if row["street_id"] is None:
                    continue
                historical = history_status == "ready"
                streets.append(ActivityImpactStreet(
                    street_id=str(row["street_id"]), city_id=str(row["city_id"]),
                    dataset_id=str(dataset_id), name=row["street_name"], city_name=row["city_name"],
                    eligible_nodes=int(row["eligible_nodes"]), supported_nodes=int(row["street_supported_nodes"]),
                    new_nodes=int(row["street_new_nodes"]) if historical else None,
                    before_nodes=int(row["before_nodes"]) if historical else None,
                    after_nodes=int(row["after_nodes"]) if historical else None,
                    completed_by_activity=bool(row["completed_by_activity"]) if historical else None,
                    current_nodes=int(row["current_nodes"]),
                    bounds=[float(row["west"]), float(row["south"]), float(row["east"]), float(row["north"])],
                ))

        return ActivityImpactPage(
            activity_id=str(activity_id), dataset_id=str(dataset_id), dataset_state=dataset["state"],
            rule=rule, coverage=coverage, history_status=history_status,
            supported_nodes=summary_supported, new_nodes=summary_new,
            streets_advanced=summary_advanced, streets_completed=summary_completed,
            streets=streets, page=page, page_size=page_size, total=total,
        )

    return router


def _calendar_date(value: Any) -> date | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_QUALIFIED_SOURCES = """
    SELECT source.account_id,source.id AS source_id,source.activity_id,source.revision
    FROM activity_sources source
    JOIN coverage_source_runs run ON run.account_id=source.account_id
      AND run.source_id=source.id AND run.source_revision=source.revision
      AND run.dataset_id=:dataset_id AND run.status='succeeded'
    JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id AND job.status='succeeded'
    WHERE source.account_id=:account_id AND source.status='succeeded' AND source.activity_id IS NOT NULL
"""

_TARGET_PROVENANCE_QUERY = f"""
    WITH qualified_sources AS ({_QUALIFIED_SOURCES})
    SELECT 1 FROM qualified_sources WHERE activity_id=:activity_id LIMIT 1
"""

_HISTORY_ACTIVITIES_QUERY = f"""
    WITH qualified_sources AS ({_QUALIFIED_SOURCES})
    SELECT DISTINCT activity.id,activity.date
    FROM qualified_sources qualified
    JOIN activities activity ON activity.id=qualified.activity_id
      AND activity.user_id=qualified.account_id
    WHERE activity.id=:activity_id OR EXISTS (
      SELECT 1 FROM source_node_contributions contribution
      WHERE contribution.account_id=qualified.account_id
        AND contribution.source_id=qualified.source_id
        AND contribution.source_revision=qualified.revision
        AND contribution.dataset_id=:dataset_id
    )
    ORDER BY activity.id
    LIMIT :history_limit
"""

_IMPACT_PAGE_QUERY = """
    WITH qualified_sources AS ({{qualified_sources}}),
    target_nodes AS MATERIALIZED (
      SELECT DISTINCT contribution.node_id
      FROM qualified_sources qualified
      JOIN source_node_contributions contribution ON contribution.account_id=qualified.account_id
        AND contribution.source_id=qualified.source_id AND contribution.source_revision=qualified.revision
        AND contribution.dataset_id=:dataset_id
      WHERE qualified.activity_id=:activity_id
    ), before_nodes AS MATERIALIZED (
      SELECT DISTINCT contribution.node_id
      FROM qualified_sources qualified
      JOIN source_node_contributions contribution ON contribution.account_id=qualified.account_id
        AND contribution.source_id=qualified.source_id AND contribution.source_revision=qualified.revision
        AND contribution.dataset_id=:dataset_id
      WHERE qualified.activity_id=ANY(:before_ids)
    ), current_nodes AS MATERIALIZED (
      {{matched_nodes}}
    ), target_streets AS (
      SELECT street.id AS street_id,street.city_id,street.display_name AS street_name,
        city.name AS city_name,street.eligible_node_count AS eligible_nodes,
        count(DISTINCT target.node_id)::integer AS street_supported_nodes,
        count(DISTINCT target.node_id) FILTER (WHERE prior.node_id IS NULL)::integer AS street_new_nodes
      FROM target_nodes target
      JOIN street_nodes street_node ON street_node.dataset_id=:dataset_id
        AND street_node.osm_node_id=target.node_id
      JOIN streets street ON street.dataset_id=street_node.dataset_id
        AND street.id=street_node.street_id AND street.eligible_node_count>0
      JOIN cities city ON city.dataset_id=street.dataset_id AND city.id=street.city_id
      LEFT JOIN before_nodes prior ON prior.node_id=target.node_id
      GROUP BY street.id,street.city_id,street.display_name,city.name,street.eligible_node_count
    ), before_streets AS (
      SELECT street_node.street_id,count(DISTINCT prior.node_id)::integer AS before_nodes
      FROM target_streets affected
      JOIN street_nodes street_node ON street_node.street_id=affected.street_id
      JOIN before_nodes prior ON prior.node_id=street_node.osm_node_id
      WHERE street_node.dataset_id=:dataset_id
      GROUP BY street_node.street_id
    ), current_streets AS (
      SELECT street_node.street_id,count(DISTINCT live.node_id)::integer AS current_nodes
      FROM target_streets affected
      JOIN street_nodes street_node ON street_node.street_id=affected.street_id
      JOIN current_nodes live ON live.node_id=street_node.osm_node_id
      WHERE street_node.dataset_id=:dataset_id
      GROUP BY street_node.street_id
    ), street_bounds AS (
      SELECT street_node.street_id,
        ST_XMin(ST_3DExtent(node.point::geometry)) AS west,
        ST_YMin(ST_3DExtent(node.point::geometry)) AS south,
        ST_XMax(ST_3DExtent(node.point::geometry)) AS east,
        ST_YMax(ST_3DExtent(node.point::geometry)) AS north
      FROM target_streets affected
      JOIN street_nodes street_node ON street_node.street_id=affected.street_id
      JOIN osm_nodes node ON node.dataset_id=street_node.dataset_id
        AND node.osm_node_id=street_node.osm_node_id
      WHERE street_node.dataset_id=:dataset_id
      GROUP BY street_node.street_id
    ), classified AS (
      SELECT target_streets.*,coalesce(before_streets.before_nodes,0) AS before_nodes,
        coalesce(before_streets.before_nodes,0)+street_new_nodes AS after_nodes,
        coalesce(current_streets.current_nodes,0) AS current_nodes,
        street_bounds.west,street_bounds.south,street_bounds.east,street_bounds.north,
        CASE WHEN :rule='strict' OR eligible_nodes<10 THEN eligible_nodes
             ELSE ceil(eligible_nodes*0.9)::integer END AS threshold
      FROM target_streets
      LEFT JOIN before_streets USING (street_id)
      LEFT JOIN current_streets USING (street_id)
      JOIN street_bounds USING (street_id)
    ), result AS (
      SELECT *, (after_nodes>before_nodes) AS advanced,
        (before_nodes < threshold AND after_nodes >= threshold) AS completed_by_activity
      FROM classified
    ), totals AS (
      SELECT (SELECT count(*) FROM target_nodes)::integer AS supported_nodes,
        (SELECT count(*) FROM target_nodes target WHERE NOT EXISTS
          (SELECT 1 FROM before_nodes prior WHERE prior.node_id=target.node_id))::integer AS new_nodes,
        count(*)::integer AS total,
        count(*) FILTER (WHERE advanced)::integer AS streets_advanced,
        count(*) FILTER (WHERE completed_by_activity)::integer AS streets_completed
      FROM result
    ), paged AS (
      SELECT * FROM result
      ORDER BY CASE WHEN :history_ready AND completed_by_activity THEN 0
                    WHEN :history_ready AND advanced THEN 1 ELSE 2 END,
        lower(city_name),lower(street_name),street_id
      LIMIT :limit OFFSET :offset
    )
    SELECT paged.*,totals.supported_nodes,totals.new_nodes,totals.total,
      totals.streets_advanced,totals.streets_completed
    FROM totals LEFT JOIN paged ON true
""".replace("{{qualified_sources}}", _QUALIFIED_SOURCES)
