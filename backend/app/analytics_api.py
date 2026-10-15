"""City Runner session handoff and private analytics preferences."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import secrets
import time
from typing import Literal
import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from fastapi.routing import APIRoute

from backend.app.auth import account_for_session_digest, utcnow
from backend.app.analytics_projection import current_projection, register_account
from backend.app.owner_guard import owner_lock


class AnalyticsBoard(BaseModel):
    id: str
    title: str


class AnalyticsStatus(BaseModel):
    state: Literal["disabled", "pending", "refreshing", "ready", "failed", "unavailable"]
    dataset_id: str | None = None
    default_dashboard_id: str | None = None
    boards: list[AnalyticsBoard] = Field(default_factory=list)


class AnalyticsPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,18}$")
    default_dashboard_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,18}$")


class AnalyticsHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination: Literal["default", "sql", "boards", "dashboard"] = "default"
    dashboard_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,18}$")


class AnalyticsHandoff(BaseModel):
    action: str
    ticket: str
    expires_in: int = 60


class AnalyticsTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Literal["activity", "exploration", "next-streets", "blank"]


class AnalyticsToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)


class AnalyticsConsume(AnalyticsToken):
    previous_token: str | None = Field(None, max_length=512)


class AnalyticsAdmission(AnalyticsToken):
    request_id: uuid.UUID
    gateway_id: uuid.UUID
    version: str = Field(max_length=512)


class AnalyticsOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnalyticsCompletion(AnalyticsOwner):
    request_id: uuid.UUID


class AnalyticsGateway(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance: uuid.UUID


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AnalyticsRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            try:
                return await handler(request)
            except SQLAlchemyError:
                raise HTTPException(503, "Analytics is temporarily unavailable.", headers={"Cache-Control": "no-store"}) from None
        return bounded


def private_call(config, owner: str, action: str, data=None):
    """Server-only Superset RPC; credentials and connection URIs never reach clients."""
    try:
        deadline = time.monotonic() + 6
        with httpx.Client(timeout=2) as client:
            with client.stream("POST", f"{config.superset_internal_url}/integration/{owner}/{action}",
                               headers={"X-Analytics-Secret": config.secret.decode()}, json=data or {}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline or len(body) + len(chunk) > 8 * 1024 * 1024:
                        raise ValueError("Oversized analytics metadata.")
                    body.extend(chunk)
        import json
        return json.loads(body)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Analytics metadata is unavailable.") from None


def create_analytics_router(engine, current_user, bearer_token, config=None):
    router = APIRouter(route_class=AnalyticsRoute)

    def enabled():
        if config is None:
            raise HTTPException(503, "Analytics is not configured.")
        return config

    def internal(response: Response, secret: str | None = Header(None, alias="X-Analytics-Secret")):
        response.headers["Cache-Control"] = "no-store"
        active = enabled()
        if secret is None or not hmac.compare_digest(secret.encode(), active.secret):
            raise HTTPException(401, "Unauthenticated.")
        return active

    def owned_boards(owner):
        state = private_call(config, owner, "state")
        return {str(board["id"]): AnalyticsBoard(id=str(board["id"]), title=board["title"])
                for board in state["boards"]}

    def revalidate(account, token, projection):
        with engine.begin() as db:
            latest = current_projection(db, config.store, account)
            if account_for_session_digest(db, digest(token)) != account:
                raise HTTPException(401, "Unauthenticated.")
            if latest is None or (latest["generation"], latest["source_revision"]) != (projection["generation"], projection["source_revision"]):
                raise HTTPException(409, "Analytics changed during the request.")

    @router.get("/api/analytics", response_model=AnalyticsStatus)
    def status(response: Response, account: str = Depends(current_user), token: str = Depends(bearer_token)):
        response.headers["Cache-Control"] = "no-store"
        if config is None:
            return AnalyticsStatus(state="disabled")
        with engine.begin() as db:
            record = register_account(db, account)
        with engine.begin() as db:
            projection = current_projection(db, config.store, account)
        state = ("ready" if projection else "refreshing" if record["state"] in {"ready", "processing"}
                 else "failed" if record["state"] == "failed" else "pending")
        boards = owned_boards(record["owner_key"]) if projection else {}
        if projection:
            revalidate(account, token, projection)
        default = str(record["default_dashboard_id"]) if record["default_dashboard_id"] is not None else None
        return AnalyticsStatus(state=state, dataset_id=str(record["selected_dataset_id"])
            if record["selected_dataset_id"] is not None else None,
            default_dashboard_id=default if default in boards else None, boards=list(boards.values()))

    @router.put("/api/analytics/preferences", status_code=204)
    def preferences(body: AnalyticsPreferences, account: str = Depends(current_user),
                    token: str = Depends(bearer_token), active=Depends(enabled)):
        with engine.begin() as db:
            record = register_account(db, account)
        if "default_dashboard_id" in body.model_fields_set and body.default_dashboard_id is not None:
            if body.default_dashboard_id not in owned_boards(record["owner_key"]):
                raise HTTPException(404, "Dashboard not found.")
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            owner_lock(db, account, exclusive=True)
            if account_for_session_digest(db, digest(token)) != account:
                raise HTTPException(401, "Unauthenticated.")
            if "dataset_id" in body.model_fields_set:
                dataset = int(body.dataset_id) if body.dataset_id is not None else None
                if dataset is not None and (dataset > 9_223_372_036_854_775_807 or not db.execute(
                        text("SELECT 1 FROM map_datasets WHERE id=:id"), {"id": dataset}).first()):
                    raise HTTPException(404, "Dataset not found.")
                db.execute(text("""UPDATE analytics_accounts SET selected_dataset_id=:dataset,
                    state='pending',generation=:generation,source_revision=NULL,lease_token=NULL,leased_until=NULL,
                    available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE account_id=:account"""), {
                    "dataset": dataset, "generation": uuid.uuid4(), "account": account,
                })
            if "default_dashboard_id" in body.model_fields_set:
                db.execute(text("UPDATE analytics_accounts SET default_dashboard_id=:id WHERE account_id=:account"), {
                    "id": int(body.default_dashboard_id) if body.default_dashboard_id else None, "account": account,
                })
        return Response(status_code=204)

    @router.post("/api/analytics/handoff", response_model=AnalyticsHandoff)
    def handoff(body: AnalyticsHandoffRequest, response: Response, account: str = Depends(current_user),
                token: str = Depends(bearer_token), active=Depends(enabled)):
        response.headers["Cache-Control"] = "no-store"
        with engine.begin() as db:
            projection = current_projection(db, active.store, account)
        if projection is None:
            raise HTTPException(409, "Analytics is refreshing or unavailable.")
        boards = owned_boards(projection["owner_key"])
        board = body.dashboard_id
        if board is not None and body.destination != "dashboard":
            raise HTTPException(422, "A dashboard is only valid for dashboard navigation.")
        if body.destination == "dashboard" and (board is None or board not in boards):
            raise HTTPException(404, "Dashboard not found.")
        ticket = secrets.token_urlsafe(32)
        session = digest(token)
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            owner_lock(db, account)
            if account_for_session_digest(db, session) != account:
                raise HTTPException(401, "Unauthenticated.")
            db.execute(text("SELECT token_digest FROM sessions WHERE token_digest=:session FOR UPDATE"), {"session": session}).one()
            db.execute(text("DELETE FROM analytics_handoff_tickets WHERE app_session_digest=:session AND expires_at<:now"), {"session": session, "now": utcnow()})
            outstanding = db.execute(text("""SELECT count(*) FROM analytics_handoff_tickets
                WHERE app_session_digest=:session AND consumed_at IS NULL AND expires_at>:now"""), {
                "session": session, "now": utcnow(),
            }).scalar_one()
            if outstanding >= 4:
                raise HTTPException(429, "Too many pending analytics handoffs.")
            db.execute(text("""INSERT INTO analytics_handoff_tickets
                (token_digest,app_session_digest,expires_at,destination,dashboard_id)
                VALUES (:ticket,:session,:expires,:destination,:board)"""), {
                "ticket": digest(ticket), "session": session, "expires": utcnow() + timedelta(seconds=60),
                "destination": body.destination, "board": int(board) if board else None,
            })
        return AnalyticsHandoff(action=active.public_origin + "/session", ticket=ticket)

    @router.post("/api/analytics/boards", response_model=AnalyticsBoard, status_code=201)
    def create_board(body: AnalyticsTemplateRequest, response: Response,
                     account: str = Depends(current_user), token: str = Depends(bearer_token), active=Depends(enabled)):
        response.headers["Cache-Control"] = "no-store"
        with engine.begin() as db:
            projection = current_projection(db, active.store, account)
        if projection is None:
            raise HTTPException(409, "Analytics is refreshing or unavailable.")
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            owner_lock(db, account)
            if account_for_session_digest(db, digest(token)) != account:
                raise HTTPException(401, "Unauthenticated.")
            board = private_call(active, projection["owner_key"], "template", {"template": body.template})
        revalidate(account, token, projection)
        return board

    def validate(db, token):
        linked = db.execute(text("""SELECT app_session_digest FROM analytics_sessions
            WHERE token_digest=:token AND revoked_at IS NULL AND expires_at>:now"""), {
            "token": digest(token), "now": utcnow(),
        }).scalar_one_or_none()
        account = account_for_session_digest(db, linked) if linked else None
        if account is None:
            raise HTTPException(401, "Unauthenticated.")
        projection = current_projection(db, config.store, account)
        if projection is None:
            raise HTTPException(409, "Analytics is refreshing or unavailable.")
        return projection

    @router.post("/internal/analytics/validate", include_in_schema=False)
    def validate_session(body: AnalyticsToken, active=Depends(internal)):
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            projection = validate(db, body.token)
            return {"owner": projection["owner_key"], "version": str(projection["generation"]) + ":" + projection["source_revision"]}

    @router.post("/internal/analytics/consume", include_in_schema=False)
    def consume(body: AnalyticsConsume, active=Depends(internal)):
        token = secrets.token_urlsafe(32)
        with engine.connect() as lookup:
            reference = lookup.execute(text("""SELECT app_session_digest FROM analytics_handoff_tickets
                WHERE token_digest=:ticket AND consumed_at IS NULL AND expires_at>:now"""), {
                "ticket": digest(body.token), "now": utcnow(),
            }).scalar_one_or_none()
            initial_account = account_for_session_digest(lookup, reference) if reference else None
        if initial_account is None:
            raise HTTPException(401, "Handoff expired or already used.")
        from backend.app.analytics_store import owner_key

        boards = owned_boards(owner_key(initial_account))
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            reference = db.execute(text("""SELECT app_session_digest FROM analytics_handoff_tickets
                WHERE token_digest=:ticket AND consumed_at IS NULL AND expires_at>:now"""), {
                "ticket": digest(body.token), "now": utcnow(),
            }).scalar_one_or_none()
            account = account_for_session_digest(db, reference) if reference else None
            if account is None or account != initial_account:
                raise HTTPException(401, "Handoff expired or already used.")
            previous_account = db.execute(text("""SELECT session.account_id FROM analytics_sessions linked
                JOIN sessions session ON session.token_digest=linked.app_session_digest
                WHERE linked.token_digest=:previous"""), {
                "previous": digest(body.previous_token) if body.previous_token else "",
            }).scalar_one_or_none()
            for owner in sorted({account, previous_account} - {None}):
                owner_lock(db, owner, exclusive=True, allow_deleted=owner != account)
            ticket = db.execute(text("""UPDATE analytics_handoff_tickets SET consumed_at=:now
                WHERE token_digest=:ticket AND consumed_at IS NULL AND expires_at>:now RETURNING *"""), {
                "ticket": digest(body.token), "now": utcnow(),
            }).mappings().first()
            account = account_for_session_digest(db, ticket["app_session_digest"]) if ticket else None
            if account is None:
                raise HTTPException(401, "Handoff expired or already used.")
            projection = current_projection(db, active.store, account)
            if projection is None:
                raise HTTPException(503, "Analytics is refreshing or unavailable.")
            if body.previous_token:
                previous = db.execute(text("""UPDATE analytics_sessions SET revoked_at=:now
                    WHERE token_digest=:previous AND revoked_at IS NULL RETURNING app_session_digest"""), {
                    "previous": digest(body.previous_token), "now": utcnow(),
                }).scalar_one_or_none()
                if previous and previous != ticket["app_session_digest"]:
                    db.execute(text("UPDATE sessions SET revoked_at=:now WHERE token_digest=:digest"), {"digest": previous, "now": utcnow()})
            count = db.execute(text("""SELECT count(*) FROM analytics_sessions linked JOIN sessions session
                ON session.token_digest=linked.app_session_digest WHERE session.account_id=:account
                AND linked.revoked_at IS NULL AND linked.expires_at>:now
                AND session.revoked_at IS NULL AND session.expires_at>:now"""), {"account": account, "now": utcnow()}).scalar_one()
            if count >= 20:
                raise HTTPException(429, "Too many active analytics sessions.")
            db.execute(text("""INSERT INTO analytics_sessions(token_digest,app_session_digest,expires_at)
                VALUES (:token,:session,:expires)"""), {
                "token": digest(token), "session": ticket["app_session_digest"], "expires": utcnow() + timedelta(minutes=30),
            })
        board = str(ticket["dashboard_id"] if ticket["destination"] == "dashboard" else projection["default_dashboard_id"])
        destination = (f"/superset/dashboard/{board}/" if board in boards and ticket["destination"] in {"default", "dashboard"}
                       else "/sqllab/" if ticket["destination"] == "sql" else "/dashboard/list/")
        return {"token": token, "destination": destination}

    @router.post("/internal/analytics/logout", include_in_schema=False, status_code=204)
    def logout(body: AnalyticsToken, active=Depends(internal)):
        with engine.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            account = db.execute(text("""SELECT parent.account_id FROM analytics_sessions linked
                JOIN sessions parent ON parent.token_digest=linked.app_session_digest
                WHERE linked.token_digest=:token"""), {"token": digest(body.token)}).scalar_one_or_none()
            if account:
                owner_lock(db, account, exclusive=True, allow_deleted=True)
            session = db.execute(text("""UPDATE analytics_sessions SET revoked_at=:now WHERE token_digest=:token
                RETURNING app_session_digest"""), {"token": digest(body.token), "now": utcnow()}).scalar_one_or_none()
            if session:
                db.execute(text("UPDATE sessions SET revoked_at=:now WHERE token_digest=:session"), {"session": session, "now": utcnow()})
        return Response(status_code=204)

    @router.post("/internal/analytics/admit", include_in_schema=False, status_code=204)
    def admit(body: AnalyticsAdmission, active=Depends(internal)):
        with engine.begin() as db:
            projection = validate(db, body.token)
            version = str(projection["generation"]) + ":" + projection["source_revision"]
            if body.version != version:
                raise HTTPException(409, "Analytics revision changed.")
        # Never hold a source lock while taking target row/FK locks: publication
        # takes them in the other order. Final validation catches intervening edits.
        with active.store.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            db.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            claimed = db.execute(text("""UPDATE control.gateway SET leased_until=clock_timestamp()+interval '30 seconds'
                WHERE singleton AND instance=:instance RETURNING singleton"""), {"instance": body.gateway_id}).first()
            if claimed is None:
                raise HTTPException(409, "Another analytics gateway owns admission.")
            inserted = db.execute(text("""INSERT INTO control.requests(request_id,owner_key)
                SELECT :request,owner_key FROM control.owners WHERE owner_key=:owner
                AND generation=:generation AND source_revision=:revision AND enabled AND NOT fenced
                RETURNING request_id"""), {
                "request": body.request_id, "owner": projection["owner_key"],
                "generation": projection["generation"], "revision": projection["source_revision"],
            }).first()
            if inserted is None:
                raise HTTPException(409, "Analytics revision changed.")
        return Response(status_code=204)

    @router.post("/internal/analytics/finish", include_in_schema=False, status_code=204)
    def finish(body: AnalyticsCompletion, active=Depends(internal)):
        with active.store.begin() as db:
            db.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            db.execute(text("DELETE FROM control.requests WHERE request_id=:request AND owner_key=:owner"), {
                "request": body.request_id, "owner": body.owner,
            })
        return Response(status_code=204)

    @router.post("/internal/analytics/fence", include_in_schema=False, status_code=204)
    def fence(body: AnalyticsOwner, active=Depends(internal)):
        with active.store.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            db.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            db.execute(text("UPDATE control.owners SET fenced=true WHERE owner_key=:owner"), {"owner": body.owner})
        return Response(status_code=204)

    @router.post("/internal/analytics/reconcile", include_in_schema=False, status_code=204)
    def reconcile(body: AnalyticsGateway, active=Depends(internal)):
        with active.store.begin() as db:
            db.exec_driver_sql("SET LOCAL statement_timeout='1500ms'")
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            if not db.execute(text("SELECT 1 FROM control.gateway WHERE singleton AND instance=:instance AND leased_until>clock_timestamp()"), {"instance": body.instance}).first():
                raise HTTPException(409, "Another analytics gateway owns admission.")
            owners = db.execute(text("""SELECT owner_key,role_name FROM control.owners owner
                WHERE EXISTS (SELECT 1 FROM control.requests request WHERE request.owner_key=owner.owner_key)
                   OR EXISTS (SELECT 1 FROM pg_stat_activity live WHERE live.usename=owner.role_name)""")).mappings().all()
            for owner in owners:
                db.execute(text("UPDATE control.owners SET fenced=true WHERE owner_key=:owner"), {"owner": owner["owner_key"]})
                db.execute(text("SELECT pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE usename=:role"), {"role": owner["role_name"]}).all()
                db.exec_driver_sql("SELECT pg_stat_clear_snapshot()")
                if db.execute(text("SELECT 1 FROM pg_stat_activity WHERE usename=:role"), {"role": owner["role_name"]}).first():
                    raise HTTPException(503, "Analytics startup reconciliation failed.")
        return Response(status_code=204)

    @router.post("/internal/analytics/claim", include_in_schema=False, status_code=204)
    def claim_gateway(body: AnalyticsGateway, active=Depends(internal)):
        with active.store.begin() as db:
            db.exec_driver_sql("SET LOCAL lock_timeout='250ms'")
            claimed = db.execute(text("""UPDATE control.gateway SET instance=:instance,
                leased_until=clock_timestamp()+interval '30 seconds' WHERE singleton
                AND (leased_until IS NULL OR leased_until<clock_timestamp() OR instance=:instance)
                RETURNING singleton"""), {"instance": body.instance}).first()
            if claimed is None:
                raise HTTPException(409, "Another analytics gateway owns admission.")
        return Response(status_code=204)

    @router.post("/internal/analytics/release", include_in_schema=False, status_code=204)
    def release_gateway(body: AnalyticsGateway, active=Depends(internal)):
        with active.store.begin() as db:
            db.execute(text("UPDATE control.gateway SET leased_until=NULL,instance=NULL WHERE instance=:instance"), {"instance": body.instance})
        return Response(status_code=204)

    return router
