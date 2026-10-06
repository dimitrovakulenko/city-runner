import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from backend.app import auth
from backend.app.schemas import ActivityDetail, ActivityPage
from backend.app.explorer import create_explorer_router
from backend.app.map_api import create_map_router
from backend.app.uploads import create_upload_router


class ChallengeRequest(BaseModel):
    provider: Literal["google", "apple"]


class ChallengeResponse(BaseModel):
    id: str
    nonce: str
    expires_at: datetime


class ExchangeRequest(BaseModel):
    challenge_id: str = Field(min_length=36, max_length=36)
    id_token: str = Field(min_length=1, max_length=16384)


class AccountResponse(BaseModel):
    id: str


class ExchangeResponse(BaseModel):
    token: str
    expires_at: datetime
    account: AccountResponse


class MeResponse(BaseModel):
    id: str


def create_app(engine: Engine | None = None, identity_resolver: Callable[..., Any] | None = None) -> FastAPI:
    engine = engine or create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg://localhost/activities"), pool_pre_ping=True)
    app = FastAPI()
    app.state.engine = engine
    bearer_docs = HTTPBearer(auto_error=False)

    def bearer_token(
        authorization: str | None = Header(None, include_in_schema=False),
        _credentials: HTTPAuthorizationCredentials | None = Security(bearer_docs),
    ) -> str:
        if not authorization:
            raise HTTPException(status_code=401, detail="Unauthenticated.")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or len(token.strip()) > 512):
            raise HTTPException(status_code=401, detail="Unauthenticated.")
        return token.strip()

    def current_user(
        authorization: str | None = Header(None, include_in_schema=False),
        _credentials: HTTPAuthorizationCredentials | None = Security(bearer_docs),
    ) -> str:
        if identity_resolver is not None:
            # Kept solely for the explicit test hook used by the activity tests.
            identity = identity_resolver()
            if not identity:
                raise HTTPException(status_code=401, detail="Unauthenticated.")
            return str(identity)
        token = bearer_token(authorization)
        with engine.connect() as db:
            identity = auth.account_for_token(db, token)
        if not identity:
            raise HTTPException(status_code=401, detail="Unauthenticated.")
        return identity

    @app.post("/api/auth/challenges", response_model=ChallengeResponse)
    def create_challenge(body: ChallengeRequest):
        with engine.begin() as db:
            return auth.new_challenge(db, body.provider)

    @app.post("/api/auth/exchange", response_model=ExchangeResponse)
    def exchange(body: ExchangeRequest):
        with engine.connect() as db:
            challenge = db.execute(text("""SELECT provider, nonce FROM login_challenges
                WHERE id=:id AND consumed_at IS NULL AND expires_at>:now"""), {
                "id": body.challenge_id, "now": auth.utcnow(),
            }).first()
        if challenge is None:
            raise HTTPException(status_code=401, detail="Login challenge is invalid, expired, or already used.")
        provider, nonce = challenge
        subject = auth.verify_id_token(provider, body.id_token, nonce)
        with engine.begin() as db:
            auth.consume_challenge(db, body.challenge_id, provider)
            return auth.create_identity_and_session(db, provider, subject)

    @app.get("/api/me", response_model=MeResponse)
    def me(account_id: str = Depends(current_user)):
        return {"id": account_id}

    @app.delete("/api/auth/session", status_code=204)
    def logout(token: str = Depends(bearer_token), account_id: str = Depends(current_user)):
        with engine.begin() as db:
            if not auth.revoke_token(db, token):
                raise HTTPException(status_code=401, detail="Unauthenticated.")
        return Response(status_code=204)

    app.include_router(create_upload_router(engine, current_user))
    app.include_router(create_map_router(engine, current_user))
    app.include_router(create_explorer_router(engine, current_user))

    @app.get("/api/activities", response_model=ActivityPage)
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

    @app.get("/api/activities/{activity_id}", response_model=ActivityDetail)
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
