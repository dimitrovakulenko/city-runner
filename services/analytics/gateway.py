"""Authenticated full Superset UI; only City Runner issues login tickets."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from services.analytics.proxy import AuthoringProxy, Identity
from backend.app.analytics_config import checked_origins

COOKIE = "city_runner_analytics"


def create_app(*, backend_url, secret, public_origin, web_origin, superset_url, transport=None):
    if len(secret.encode()) < 32:
        raise ValueError("Analytics requires a server secret of at least 32 bytes.")
    public_origin, web_origin = checked_origins(public_origin, web_origin)
    secure = urlsplit(public_origin).scheme == "https"
    instance = None
    rpc_slots, rpc_pending = asyncio.Semaphore(2), 0

    async def rpc(action, data, timeout=1.5):
        nonlocal rpc_pending
        if rpc_pending >= 64:
            raise HTTPException(503, "Analytics authentication is busy.")
        rpc_pending += 1
        try:
            # Bound cold asset bursts before they reach the backend's four-slot
            # database pools. Queueing remains inside the existing RPC deadline.
            async with asyncio.timeout(timeout), rpc_slots, httpx.AsyncClient(transport=transport, timeout=timeout) as client:
                async with client.stream("POST", f"{backend_url}/internal/analytics/{action}", json=data,
                                         headers={"X-Analytics-Secret": secret}) as result:
                    if result.status_code in (401, 409):
                        raise HTTPException(result.status_code)
                    result.raise_for_status()
                    body = bytearray()
                    async for chunk in result.aiter_bytes():
                        if len(body) + len(chunk) > 65536:
                            raise ValueError("Oversized authentication response.")
                        body.extend(chunk)
            return json.loads(body) if body else None
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise HTTPException(503, "Analytics authentication is unavailable.") from None
        finally:
            rpc_pending -= 1

    async def resolve(token):
        if not token or len(token) > 512:
            return None
        try:
            profile = await rpc("validate", {"token": token})
            return Identity(profile["owner"], profile["version"])
        except HTTPException as error:
            if error.status_code in (401, 409):
                return None
            raise

    async def fence(owner):
        await rpc("fence", {"owner": owner})

    async def admit(identity, token, request_id):
        await rpc("admit", {"token": token, "request_id": request_id, "version": identity.version, "gateway_id": instance})

    async def finish(identity, request_id):
        await rpc("finish", {"owner": identity.owner, "request_id": request_id})

    def check_origin(request):
        if request.headers.get("origin") not in (None, public_origin) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal instance
        instance = str(uuid.uuid4())  # Generated after fork, even with preloading.
        # Interrupted admissions remain durable even if their DB query finished.
        # An unclean restart fences them before any new authoring is accepted.
        deadline = time.monotonic() + 35
        while True:
            try:
                await rpc("claim", {"instance": instance})
                break
            except HTTPException as error:
                if error.status_code != 409 or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(1)
        await rpc("reconcile", {"instance": instance}, timeout=8)
        try:
            yield
        finally:
            try:
                await rpc("release", {"instance": instance})
            except HTTPException:
                pass  # A crashed/unreachable gateway lease expires after 30s.

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    boundary = AuthoringProxy(resolve=resolve, fence=fence, admit=admit, finish=finish,
        check_origin=check_origin, cookie=COOKIE, upstream=superset_url, public_origin=public_origin)
    app.state.boundary = boundary

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/session")
    async def session(request: Request):
        if request.headers.get("origin") != web_origin:
            raise HTTPException(403)
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415)
        body = bytearray()
        try:
            async with asyncio.timeout(2):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 1024:
                        raise HTTPException(413)
                    body.extend(chunk)
        except TimeoutError:
            raise HTTPException(408) from None
        try:
            fields = parse_qs(body.decode(), strict_parsing=True, max_num_fields=1)
            ticket, = fields["ticket"]
        except (ValueError, UnicodeError, KeyError):
            raise HTTPException(400) from None
        if not ticket or len(ticket) > 512:
            raise HTTPException(400)
        issued = await rpc("consume", {"token": ticket, "previous_token": request.cookies.get(COOKIE)}, timeout=8)
        response = Response(status_code=303, headers={"Location": issued["destination"], "Cache-Control": "no-store"})
        response.set_cookie(COOKIE, issued["token"], httponly=True, secure=secure, samesite="strict", max_age=1800)
        # Keep Superset's CSRF session stable across same-account tabs. The
        # request identity adapter rebinds its login on an account replacement.
        return response

    @app.get("/logout")
    @app.get("/logout/")
    async def logout(request: Request):
        check_origin(request)
        token = request.cookies.get(COOKIE)
        if token:
            await rpc("logout", {"token": token})
        response = Response(status_code=303, headers={"Location": web_origin, "Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, secure=secure, samesite="strict")
        response.delete_cookie("session", secure=secure, samesite="strict")
        return response

    app.add_api_route("/{path:path}", boundary.proxy,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    return app


def configured_app():
    return create_app(backend_url=os.environ["ANALYTICS_BACKEND_URL"].rstrip("/"),
        secret=os.environ["ANALYTICS_SECRET"], public_origin=os.environ["ANALYTICS_PUBLIC_ORIGIN"].rstrip("/"),
        web_origin=os.environ["WEB_ORIGIN"].rstrip("/"), superset_url=os.environ["SUPERSET_INTERNAL_URL"].rstrip("/"))
