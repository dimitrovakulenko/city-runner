"""Authenticated, version-qualified city and street browsing APIs."""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


MAX_BIGINT = 9_223_372_036_854_775_807
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
STATEMENT_TIMEOUT_MS = 1500
Rule = Literal["normal", "strict"]
StreetFilter = Literal["all", "incomplete", "partial", "completed", "nearly-complete"]
StreetSort = Literal["name", "completion-desc", "completion-asc", "remaining-asc"]


class DatasetCoverage(BaseModel):
    status: Literal["ready", "pending", "failed"]
    progress_revision: str
    pending_sources: int
    failed_sources: int
    pending_imports: int
    visited_node_count: int | None
    unsupported_sample_count: int | None


class PageInfo(BaseModel):
    page: int
    page_size: int
    total: int | None


class CityItem(BaseModel):
    id: str
    name: str
    admin_level: str
    visited_nodes: int | None
    eligible_nodes: int | None
    completed_streets: int | None
    manual_completed_streets: int
    effective_completed_streets: int | None
    eligible_streets: int | None


class CityPage(BaseModel):
    dataset_id: str
    dataset_state: Literal["active", "importing", "retired"]
    rule: Rule
    coverage: DatasetCoverage
    items: list[CityItem]
    page: int
    page_size: int
    total: int


class StreetItem(BaseModel):
    id: str
    dataset_id: str
    city_id: str
    name: str
    visited_nodes: int | None
    eligible_nodes: int | None
    threshold: int | None
    state: Literal["complete", "partial", "missing"] | None
    manual_completed: bool
    manual_reason: str | None
    effective_state: Literal["complete", "partial", "missing"] | None


class StreetPage(BaseModel):
    dataset_id: str
    city_id: str
    dataset_state: Literal["active", "importing", "retired"]
    rule: Rule
    coverage: DatasetCoverage
    filter: StreetFilter
    filter_applied: bool
    sort: StreetSort
    sort_applied: bool
    items: list[StreetItem]
    page: int
    page_size: int
    total: int


class RemainingNode(BaseModel):
    id: str
    longitude: float
    latitude: float


class StreetDetail(BaseModel):
    id: str
    dataset_id: str
    city_id: str
    name: str
    dataset_state: Literal["active", "importing", "retired"]
    rule: Rule
    coverage: DatasetCoverage
    visited_nodes: int | None
    eligible_nodes: int | None
    threshold: int | None
    state: Literal["complete", "partial", "missing"] | None
    manual_completed: bool
    manual_reason: str | None
    effective_state: Literal["complete", "partial", "missing"] | None
    remaining_nodes: list[RemainingNode] | None
    remaining_nodes_page: PageInfo


class ContributingActivity(BaseModel):
    id: str
    name: str
    date: str
    type: str
    supported_nodes: int


class ContributionPage(BaseModel):
    dataset_id: str
    street_id: str
    dataset_state: Literal["active", "importing", "retired"]
    coverage: DatasetCoverage
    activities_available: bool
    activities: list[ContributingActivity]
    page: int
    page_size: int
    total: int | None


def create_explorer_router(engine: Engine, current_user: Callable[..., str]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/cities", response_model=CityPage)
    def cities(
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        rule: Rule = Query("normal"),
        page: int = Query(1, ge=1, le=1_000_000),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        q: str | None = Query(None, max_length=200),
        account_id: str = Depends(current_user),
    ):
        with _snapshot(engine) as db:
            dataset = _dataset(db, dataset_id)
            coverage = _coverage(db, account_id, dataset_id, dataset)
            rows = db.execute(text(_CITY_LIST_QUERY), {
                "dataset_id": dataset_id, "q": q or "", "limit": page_size,
                "offset": (page - 1) * page_size,
            }).mappings().all()
            total = int(rows[0]["total"]) if rows else _city_count(db, dataset_id, q)
            city_ids = [int(row["id"]) for row in rows]
            progress = {}
            if city_ids and coverage.status == "ready":
                progress = _city_progress(db, account_id, dataset_id, city_ids, rule)
            elif city_ids:
                progress = _city_manual_counts(db, account_id, dataset_id, city_ids)
            items = []
            for row in rows:
                stats = progress.get(int(row["id"]))
                items.append(CityItem(
                    id=str(row["id"]), name=row["name"], admin_level=row["admin_level"],
                    visited_nodes=stats["visited_nodes"] if stats and coverage.status == "ready" else None,
                    eligible_nodes=stats["eligible_nodes"] if stats and coverage.status == "ready" else None,
                    completed_streets=stats["completed_streets"] if stats and coverage.status == "ready" else None,
                    manual_completed_streets=stats["manual_completed_streets"] if stats else 0,
                    effective_completed_streets=stats["effective_completed_streets"]
                    if stats and coverage.status == "ready" else None,
                    eligible_streets=stats["eligible_streets"] if stats and coverage.status == "ready" else None,
                ))
        return CityPage(dataset_id=str(dataset_id), dataset_state=dataset["state"], rule=rule,
                        coverage=coverage, items=items, page=page, page_size=page_size, total=total)

    @router.get("/api/cities/{city_id}/streets", response_model=StreetPage)
    def city_streets(
        city_id: int = Path(ge=1, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        rule: Rule = Query("normal"),
        filter: StreetFilter = Query("all"),
        sort: StreetSort = Query("name"),
        page: int = Query(1, ge=1, le=1_000_000),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        q: str | None = Query(None, max_length=200),
        account_id: str = Depends(current_user),
    ):
        with _snapshot(engine) as db:
            dataset = _dataset(db, dataset_id)
            _city(db, dataset_id, city_id)
            coverage = _coverage(db, account_id, dataset_id, dataset)
            ready = coverage.status == "ready"
            params = {
                "dataset_id": dataset_id, "city_id": city_id, "account_id": account_id,
                "q": q or "", "filter": filter, "rule": rule, "limit": page_size,
                "offset": (page - 1) * page_size, "sort": sort,
            }
            query = _STREET_LIST_QUERY_READY if ready else _STREET_LIST_QUERY_PENDING
            rows = db.execute(text(query), params).mappings().all()
            total = int(rows[0]["total"]) if rows else _street_count(
                db, dataset_id, city_id, account_id, q, rule, filter, ready
            )
            items = [StreetItem(
                id=str(row["id"]), dataset_id=str(dataset_id), city_id=str(city_id),
                name=row["name"], visited_nodes=row["visited_nodes"],
                eligible_nodes=row["eligible_nodes"], threshold=row["threshold"], state=row["state"],
                manual_completed=bool(row["manual_completed"]), manual_reason=row["manual_reason"],
                effective_state=row["effective_state"],
            ) for row in rows]
        return StreetPage(dataset_id=str(dataset_id), city_id=str(city_id),
                          dataset_state=dataset["state"], rule=rule, coverage=coverage,
                          filter=filter, filter_applied=ready or filter == "all",
                          sort=sort, sort_applied=ready or sort == "name",
                          items=items, page=page, page_size=page_size, total=total)

    @router.get("/api/streets/{street_id}", response_model=StreetDetail)
    def street_detail(
        street_id: int = Path(ge=1, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        rule: Rule = Query("normal"),
        page: int = Query(1, ge=1, le=1_000_000),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        account_id: str = Depends(current_user),
    ):
        with _snapshot(engine) as db:
            dataset = _dataset(db, dataset_id)
            street = _street(db, dataset_id, street_id)
            coverage = _coverage(db, account_id, dataset_id, dataset)
            if street["eligible_node_count"] == 0:
                raise HTTPException(status_code=404, detail="Street not found.")
            if coverage.status == "ready":
                counts = _street_counts(db, account_id, dataset_id, street_id, dataset, rule)
                missing = _remaining_nodes_page(
                    db, account_id, dataset_id, street_id, page, page_size
                )
                visited, eligible, threshold, state = (
                    counts["visited_nodes"], counts["eligible_nodes"],
                    counts["threshold"], counts["state"],
                )
                remaining, total = missing
            else:
                visited = eligible = threshold = state = None
                remaining, total = None, None
            manual = _manual_completion(db, account_id, dataset_id, street_id)
            effective_state = "complete" if manual else state
        return StreetDetail(
            id=str(street_id), dataset_id=str(dataset_id), city_id=str(street["city_id"]),
            name=street["name"], dataset_state=dataset["state"], rule=rule, coverage=coverage,
            visited_nodes=visited, eligible_nodes=eligible, threshold=threshold, state=state,
            manual_completed=manual is not None, manual_reason=manual["reason"] if manual else None,
            effective_state=effective_state,
            remaining_nodes=remaining,
            remaining_nodes_page=PageInfo(page=page, page_size=page_size, total=total),
        )

    @router.get("/api/streets/{street_id}/contributions", response_model=ContributionPage)
    def contributions(
        street_id: int = Path(ge=1, le=MAX_BIGINT),
        dataset_id: int = Query(ge=1, le=MAX_BIGINT),
        page: int = Query(1, ge=1, le=1_000_000),
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        account_id: str = Depends(current_user),
    ):
        with _snapshot(engine) as db:
            dataset = _dataset(db, dataset_id)
            street = _street(db, dataset_id, street_id)
            if street["eligible_node_count"] == 0:
                raise HTTPException(status_code=404, detail="Street not found.")
            coverage = _coverage(db, account_id, dataset_id, dataset)
            if coverage.status != "ready":
                activities, total = [], None
            else:
                params = {"dataset_id": dataset_id, "street_id": street_id,
                          "account_id": account_id, "limit": page_size,
                          "offset": (page - 1) * page_size}
                rows = db.execute(text(_CONTRIBUTIONS_QUERY), params).mappings().all()
                total = int(rows[0]["total"]) if rows else _contribution_count(
                    db, dataset_id, street_id, account_id
                )
                activities = [ContributingActivity(
                    id=str(row["id"]), name=row["name"], date=row["date"] or "unknown",
                    type=row["activity_type"] or "unknown", supported_nodes=int(row["supported_nodes"]),
                ) for row in rows]
        return ContributionPage(
            dataset_id=str(dataset_id), street_id=str(street_id), dataset_state=dataset["state"],
            coverage=coverage, activities_available=coverage.status == "ready",
            activities=activities, page=page, page_size=page_size, total=total,
        )

    return router


@contextmanager
def _snapshot(engine: Engine):
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as db:
            with db.begin():
                db.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
                yield db
    except DBAPIError as error:
        sqlstate = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
        if sqlstate == "57014":
            raise HTTPException(status_code=503,
                                detail="City query exceeded its work limit; retry with a smaller page.") from error
        raise


def _dataset(db, dataset_id: int) -> dict[str, Any]:
    row = db.execute(text("SELECT status,coverage_mode FROM map_datasets WHERE id=:id"), {
        "id": dataset_id,
    }).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return {"state": row["status"], "coverage_mode": row["coverage_mode"]}


def _city(db, dataset_id: int, city_id: int):
    row = db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset AND id=:city"), {
        "dataset": dataset_id, "city": city_id,
    }).first()
    if row is None:
        raise HTTPException(status_code=404, detail="City not found.")


def _street(db, dataset_id: int, street_id: int):
    row = db.execute(text("""SELECT id,city_id,display_name AS name,eligible_node_count
        FROM streets WHERE dataset_id=:dataset AND id=:street"""), {
        "dataset": dataset_id, "street": street_id,
    }).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Street not found.")
    return row


def _coverage(db, account_id: str, dataset_id: int, dataset: dict[str, Any]) -> DatasetCoverage:
    from backend.app.coverage import get_account_coverage

    if dataset["state"] == "active" and dataset["coverage_mode"] == "complete":
        live = get_account_coverage(db, account_id, dataset_id)
    else:
        live = _version_coverage(db, account_id, dataset_id, dataset["coverage_mode"])
    ready = live["status"] == "ready"
    return DatasetCoverage(
        status=live["status"], progress_revision=str(live["progress_revision"]),
        pending_sources=int(live["pending_sources"]), failed_sources=int(live["failed_sources"]),
        pending_imports=int(db.execute(text("""SELECT count(*) FROM activity_sources
            WHERE account_id=:account AND status IN ('queued','processing')"""), {
            "account": account_id,
        }).scalar_one()),
        visited_node_count=int(live["visited_node_count"]) if ready else None,
        unsupported_sample_count=int(live["unsupported_sample_count"]) if ready else None,
    )


def _version_coverage(db, account_id: str, dataset_id: int, coverage_mode: str) -> dict[str, Any]:
    rows = db.execute(text("""WITH current_sources AS (
            SELECT source.id,run.status AS run_status,job.status AS job_status,
                   run.unsupported_sample_count
            FROM activity_sources source
            LEFT JOIN coverage_source_runs run ON run.account_id=source.account_id
              AND run.source_id=source.id AND run.source_revision=source.revision
              AND run.dataset_id=:dataset
            LEFT JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id
            WHERE source.account_id=:account AND source.status='succeeded'
              AND source.activity_id IS NOT NULL
        )
        SELECT count(*) FILTER (WHERE run_status='failed' OR job_status IN ('failed','cancelled')) AS failed,
          count(*) FILTER (WHERE run_status IS NULL OR run_status<>'succeeded'
               OR job_status IS DISTINCT FROM 'succeeded') AS pending,
          COALESCE(sum(unsupported_sample_count) FILTER (
               WHERE run_status='succeeded' AND job_status='succeeded'),0) AS unsupported
        FROM current_sources"""), {"dataset": dataset_id, "account": account_id}).mappings().one()
    visited = db.execute(text("""SELECT count(DISTINCT contribution.node_id)
        FROM source_node_contributions contribution
        JOIN activity_sources source ON source.account_id=contribution.account_id
          AND source.id=contribution.source_id AND source.revision=contribution.source_revision
          AND source.status='succeeded' AND source.activity_id IS NOT NULL
        JOIN coverage_source_runs run ON run.account_id=contribution.account_id
          AND run.source_id=contribution.source_id AND run.source_revision=contribution.source_revision
          AND run.dataset_id=contribution.dataset_id AND run.status='succeeded'
        JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id AND job.status='succeeded'
        WHERE contribution.account_id=:account AND contribution.dataset_id=:dataset"""), {
        "account": account_id, "dataset": dataset_id,
    }).scalar_one()
    progress_revision = db.execute(text("""SELECT progress_revision FROM account_dataset_coverage
        WHERE account_id=:account AND dataset_id=:dataset"""), {
        "account": account_id, "dataset": dataset_id,
    }).scalar_one_or_none() or 0
    failed, pending = int(rows["failed"]), int(rows["pending"])
    if coverage_mode != "complete":
        status = "pending"
    elif failed:
        status = "failed"
    elif pending:
        status = "pending"
    else:
        status = "ready"
    return {"status": status, "pending_sources": pending, "failed_sources": failed,
            "visited_node_count": int(visited) if status == "ready" else None,
            "unsupported_sample_count": int(rows["unsupported"]) if status == "ready" else None,
            "progress_revision": int(progress_revision)}


def _city_count(db, dataset_id: int, query: str | None) -> int:
    return int(db.execute(text("""SELECT count(*) FROM cities
        WHERE dataset_id=:dataset AND (:q='' OR strpos(lower(name),lower(:q))>0)"""), {
        "dataset": dataset_id, "q": query or "",
    }).scalar_one())


def _city_progress(db, account_id: str, dataset_id: int, city_ids: list[int], rule: Rule):
    threshold = _threshold_sql("sp.eligible_node_count", rule)
    rows = db.execute(text(f"""WITH matched_nodes AS MATERIALIZED (
          {_MATCHED_NODES_SQL}
        ), street_progress AS (
          SELECT s.id,s.city_id,s.eligible_node_count,
            count(DISTINCT sn.osm_node_id) FILTER (WHERE mn.node_id IS NOT NULL)::integer AS visited_nodes
          FROM streets s LEFT JOIN street_nodes sn ON sn.dataset_id=s.dataset_id AND sn.street_id=s.id
          LEFT JOIN matched_nodes mn ON mn.node_id=sn.osm_node_id
          WHERE s.dataset_id=:dataset AND s.city_id=ANY(:city_ids) AND s.eligible_node_count>0
          GROUP BY s.id,s.city_id,s.eligible_node_count
        ), street_totals AS (
          SELECT sp.city_id,count(*) AS eligible_streets,
            count(*) FILTER (WHERE visited_nodes >= {threshold}) AS completed_streets,
            count(*) FILTER (WHERE manual.street_id IS NOT NULL) AS manual_completed_streets,
            count(*) FILTER (WHERE visited_nodes >= {threshold} OR manual.street_id IS NOT NULL)
              AS effective_completed_streets
          FROM street_progress sp LEFT JOIN manual_street_completions manual
            ON manual.account_id=:account_id AND manual.dataset_id=:dataset AND manual.street_id=sp.id
          GROUP BY sp.city_id
        ), city_nodes AS (
          SELECT s.city_id,count(DISTINCT sn.osm_node_id) AS eligible_nodes,
            count(DISTINCT sn.osm_node_id) FILTER (WHERE mn.node_id IS NOT NULL) AS visited_nodes
          FROM streets s JOIN street_nodes sn ON sn.dataset_id=s.dataset_id AND sn.street_id=s.id
          LEFT JOIN matched_nodes mn ON mn.node_id=sn.osm_node_id
          WHERE s.dataset_id=:dataset AND s.city_id=ANY(:city_ids) AND s.eligible_node_count>0
          GROUP BY s.city_id
        )
        SELECT city.id AS city_id,COALESCE(n.visited_nodes,0) AS visited_nodes,
          COALESCE(n.eligible_nodes,0) AS eligible_nodes,
          COALESCE(t.completed_streets,0) AS completed_streets,
          COALESCE(t.manual_completed_streets,0) AS manual_completed_streets,
          COALESCE(t.effective_completed_streets,0) AS effective_completed_streets,
          COALESCE(t.eligible_streets,0) AS eligible_streets
        FROM cities city LEFT JOIN city_nodes n ON n.city_id=city.id
        LEFT JOIN street_totals t ON t.city_id=city.id
        WHERE city.dataset_id=:dataset AND city.id=ANY(:city_ids)"""), {
        "account_id": account_id, "dataset_id": dataset_id, "dataset": dataset_id,
        "city_ids": city_ids,
    }).mappings().all()
    return {int(row["city_id"]): {key: int(row[key]) for key in (
        "visited_nodes", "eligible_nodes", "completed_streets", "manual_completed_streets",
        "effective_completed_streets", "eligible_streets",
    )} for row in rows}


def _city_manual_counts(db, account_id: str, dataset_id: int, city_ids: list[int]):
    rows = db.execute(text("""SELECT city.id AS city_id,count(manual.street_id)::integer AS manual_completed_streets
        FROM cities city LEFT JOIN streets street ON street.dataset_id=city.dataset_id
          AND street.city_id=city.id AND street.eligible_node_count>0
        LEFT JOIN manual_street_completions manual ON manual.account_id=:account
          AND manual.dataset_id=street.dataset_id AND manual.street_id=street.id
        WHERE city.dataset_id=:dataset AND city.id=ANY(:city_ids)
        GROUP BY city.id"""), {
        "account": account_id, "dataset": dataset_id, "city_ids": city_ids,
    }).mappings().all()
    return {int(row["city_id"]): {"manual_completed_streets": int(row["manual_completed_streets"])}
            for row in rows}


def _threshold_sql(total: str, rule: Rule) -> str:
    if rule == "strict":
        return total
    return f"CASE WHEN {total}<10 THEN {total} ELSE ceil({total}*0.9)::integer END"


def _street_count(db, dataset_id: int, city_id: int, account_id: str,
                  query: str | None, rule: Rule, filter_value: StreetFilter, ready: bool) -> int:
    statement = _STREET_COUNT_READY if ready else _STREET_COUNT_PENDING
    return int(db.execute(text(statement), {
        "dataset_id": dataset_id, "city_id": city_id, "account_id": account_id,
        "q": query or "", "filter": filter_value, "rule": rule,
    }).scalar_one())


def _street_counts(db, account_id: str, dataset_id: int, street_id: int,
                   dataset: dict[str, Any], rule: Rule) -> dict[str, Any]:
    if dataset["state"] == "active":
        from backend.app.coverage import get_street_coverage
        result = get_street_coverage(db, account_id, dataset_id, street_id, rule)
        return {"visited_nodes": result["visited"], "eligible_nodes": result["total"],
                "threshold": result["threshold"], "state": result["state"]}
    total = int(db.execute(text("SELECT eligible_node_count FROM streets WHERE dataset_id=:dataset AND id=:id"), {
        "dataset": dataset_id, "id": street_id,
    }).scalar_one())
    visited = int(db.execute(text(f"""SELECT count(*) FROM street_nodes sn
        JOIN ({_MATCHED_NODES_SQL}) mn ON mn.node_id=sn.osm_node_id
        WHERE sn.dataset_id=:dataset AND sn.street_id=:street"""), {
        "account_id": account_id, "dataset_id": dataset_id,
        "dataset": dataset_id, "street": street_id,
    }).scalar_one())
    threshold = total if rule == "strict" or total < 10 else math.ceil(total * 0.9)
    state = "complete" if total > 0 and visited >= threshold else "missing" if visited == 0 else "partial"
    return {"visited_nodes": visited, "eligible_nodes": total, "threshold": threshold, "state": state}


def _remaining_nodes_page(db, account_id: str, dataset_id: int, street_id: int,
                          page: int, page_size: int):
    params = {"account_id": account_id, "dataset_id": dataset_id, "street_id": street_id,
              "limit": page_size, "offset": (page - 1) * page_size}
    total = int(db.execute(text(f"""SELECT count(*) FROM street_nodes sn
        WHERE sn.dataset_id=:dataset_id AND sn.street_id=:street_id
          AND NOT EXISTS (SELECT 1 FROM ({_MATCHED_NODES_SQL}) mn WHERE mn.node_id=sn.osm_node_id)"""),
        params).scalar_one())
    rows = db.execute(text(f"""SELECT sn.osm_node_id AS id,ST_X(node.point::geometry) AS longitude,
          ST_Y(node.point::geometry) AS latitude
        FROM street_nodes sn JOIN osm_nodes node
          ON node.dataset_id=sn.dataset_id AND node.osm_node_id=sn.osm_node_id
        WHERE sn.dataset_id=:dataset_id AND sn.street_id=:street_id
          AND NOT EXISTS (SELECT 1 FROM ({_MATCHED_NODES_SQL}) mn WHERE mn.node_id=sn.osm_node_id)
        ORDER BY sn.osm_node_id LIMIT :limit OFFSET :offset"""), params).mappings().all()
    return [RemainingNode(id=str(row["id"]), longitude=row["longitude"], latitude=row["latitude"])
            for row in rows], total


def _contribution_count(db, dataset_id: int, street_id: int, account_id: str) -> int:
    return int(db.execute(text(f"""SELECT count(DISTINCT activity.id)
        FROM ({_STREET_MATCHES_SQL}) matched
        JOIN activities activity ON activity.user_id=:account_id AND activity.id=matched.activity_id"""), {
        "dataset_id": dataset_id, "street_id": street_id, "account_id": account_id,
    }).scalar_one())


_CITY_LIST_QUERY = """
    SELECT id,name,admin_level,count(*) OVER() AS total FROM cities
    WHERE dataset_id=:dataset_id AND (:q='' OR strpos(lower(name),lower(:q))>0)
    ORDER BY lower(name),id LIMIT :limit OFFSET :offset
"""


_MATCHED_NODES_SQL = """
    SELECT DISTINCT contribution.node_id
    FROM source_node_contributions contribution
    JOIN activity_sources source ON source.account_id=contribution.account_id
      AND source.id=contribution.source_id AND source.revision=contribution.source_revision
      AND source.status='succeeded' AND source.activity_id IS NOT NULL
    JOIN coverage_source_runs run ON run.account_id=contribution.account_id
      AND run.source_id=contribution.source_id AND run.source_revision=contribution.source_revision
      AND run.dataset_id=contribution.dataset_id AND run.status='succeeded'
    JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id AND job.status='succeeded'
    WHERE contribution.account_id=:account_id AND contribution.dataset_id=:dataset_id
"""


_STREET_SELECT_READY = f"""
    WITH matched_nodes AS MATERIALIZED ({_MATCHED_NODES_SQL}), counts AS (
      SELECT s.id,s.display_name AS name,s.eligible_node_count,
        manual.reason AS manual_reason,
        count(DISTINCT sn.osm_node_id) FILTER (WHERE mn.node_id IS NOT NULL)::integer AS visited_nodes
      FROM streets s LEFT JOIN street_nodes sn ON sn.dataset_id=s.dataset_id AND sn.street_id=s.id
      LEFT JOIN matched_nodes mn ON mn.node_id=sn.osm_node_id
      LEFT JOIN manual_street_completions manual ON manual.account_id=:account_id
        AND manual.dataset_id=s.dataset_id AND manual.street_id=s.id
      WHERE s.dataset_id=:dataset_id AND s.city_id=:city_id AND s.eligible_node_count>0
        AND (:q='' OR strpos(lower(s.display_name),lower(:q))>0)
      GROUP BY s.id,s.display_name,s.eligible_node_count,manual.reason
    ), classified AS (
      SELECT id,name,eligible_node_count AS eligible_nodes,visited_nodes,manual_reason,
        CASE WHEN :rule='strict' OR eligible_node_count<10 THEN eligible_node_count
             ELSE ceil(eligible_node_count*0.9)::integer END AS threshold,
        CASE WHEN visited_nodes >= CASE WHEN :rule='strict' OR eligible_node_count<10
                    THEN eligible_node_count ELSE ceil(eligible_node_count*0.9)::integer END
             THEN 'complete' WHEN visited_nodes=0 THEN 'missing' ELSE 'partial' END AS state
        ,CASE WHEN manual_reason IS NOT NULL THEN 'complete'
             WHEN visited_nodes >= CASE WHEN :rule='strict' OR eligible_node_count<10
                    THEN eligible_node_count ELSE ceil(eligible_node_count*0.9)::integer END
             THEN 'complete' WHEN visited_nodes=0 THEN 'missing' ELSE 'partial' END AS effective_state
      FROM counts
    ), filtered AS (
      SELECT *,count(*) OVER() AS total FROM classified
      WHERE :filter='all' OR (:filter='completed' AND effective_state='complete')
        OR (:filter='partial' AND manual_reason IS NULL AND state='partial')
        OR (:filter='incomplete' AND manual_reason IS NULL AND state IN ('partial','missing'))
        OR (:filter='nearly-complete' AND manual_reason IS NULL
            AND visited_nodes::numeric / eligible_nodes >= 0.8 AND visited_nodes < threshold)
    )
"""

_STREET_LIST_QUERY_READY = _STREET_SELECT_READY + """
    SELECT id,name,visited_nodes,eligible_nodes,threshold,state,manual_reason,
      (manual_reason IS NOT NULL) AS manual_completed,effective_state,total FROM filtered
    ORDER BY
      CASE WHEN :sort='completion-desc' THEN visited_nodes::numeric / eligible_nodes END DESC,
      CASE WHEN :sort='completion-asc' THEN visited_nodes::numeric / eligible_nodes END ASC,
      CASE WHEN :sort='remaining-asc' THEN eligible_nodes-visited_nodes END ASC,
      lower(name),id LIMIT :limit OFFSET :offset
"""

_STREET_COUNT_READY = _STREET_SELECT_READY + "SELECT count(*) FROM filtered"

_STREET_LIST_QUERY_PENDING = """
    SELECT s.id,s.display_name AS name,NULL::integer AS visited_nodes,NULL::integer AS eligible_nodes,
      NULL::integer AS threshold,NULL::text AS state,manual.reason AS manual_reason,
      (manual.reason IS NOT NULL) AS manual_completed,
      CASE WHEN manual.reason IS NOT NULL THEN 'complete' END::text AS effective_state,
      count(*) OVER() AS total
    FROM streets s LEFT JOIN manual_street_completions manual ON manual.account_id=:account_id
      AND manual.dataset_id=s.dataset_id AND manual.street_id=s.id
    WHERE s.dataset_id=:dataset_id AND s.city_id=:city_id AND s.eligible_node_count>0
      AND (:q='' OR strpos(lower(display_name),lower(:q))>0)
    ORDER BY lower(s.display_name),s.id LIMIT :limit OFFSET :offset
"""


def _manual_completion(db, account_id: str, dataset_id: int, street_id: int):
    return db.execute(text("""SELECT reason FROM manual_street_completions
        WHERE account_id=:account AND dataset_id=:dataset AND street_id=:street"""), {
        "account": account_id, "dataset": dataset_id, "street": street_id,
    }).mappings().first()

_STREET_COUNT_PENDING = """
    SELECT count(*) FROM streets WHERE dataset_id=:dataset_id AND city_id=:city_id
      AND eligible_node_count>0 AND (:q='' OR strpos(lower(display_name),lower(:q))>0)
"""

_STREET_MATCHES_SQL = f"""
    SELECT DISTINCT source.activity_id,contribution.node_id
    FROM street_nodes street
    JOIN source_node_contributions contribution ON contribution.dataset_id=street.dataset_id
      AND contribution.node_id=street.osm_node_id AND contribution.account_id=:account_id
    JOIN activity_sources source ON source.account_id=contribution.account_id
      AND source.id=contribution.source_id AND source.revision=contribution.source_revision
      AND source.status='succeeded' AND source.activity_id IS NOT NULL
    JOIN coverage_source_runs run ON run.account_id=contribution.account_id
      AND run.source_id=contribution.source_id AND run.source_revision=contribution.source_revision
      AND run.dataset_id=contribution.dataset_id AND run.status='succeeded'
    JOIN jobs job ON job.account_id=run.account_id AND job.id=run.job_id AND job.status='succeeded'
    WHERE street.dataset_id=:dataset_id AND street.street_id=:street_id
"""

_CONTRIBUTIONS_QUERY = f"""
    WITH matched AS ({_STREET_MATCHES_SQL}), activity_rollup AS (
      SELECT activity.id,activity.name,activity.date,activity.activity_type,
        count(DISTINCT matched.node_id)::integer AS supported_nodes
      FROM matched JOIN activities activity ON activity.user_id=:account_id
        AND activity.id=matched.activity_id
      GROUP BY activity.id,activity.name,activity.date,activity.activity_type
    )
    SELECT id,name,date,activity_type,supported_nodes,count(*) OVER() AS total
    FROM activity_rollup
    ORDER BY CASE WHEN date IS NULL OR date='' OR date='unknown' THEN 1 ELSE 0 END,date DESC,id DESC
    LIMIT :limit OFFSET :offset
"""
