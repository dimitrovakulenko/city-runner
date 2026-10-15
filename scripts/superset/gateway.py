"""Loopback-only synthetic City Runner session / full Superset UI pilot."""

import asyncio
import secrets
import time
import re
import threading
import os
from http.cookies import SimpleCookie
from datetime import timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from backend.app.auth import account_for_token, revoke_token, utcnow


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
SESSION_DATABASE = os.getenv('PILOT_SESSION_DATABASE_URL')
APP_URL = os.getenv('PILOT_APP_URL')
engine = (create_engine(SESSION_DATABASE, pool_pre_ping=True) if SESSION_DATABASE else
          create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool))
SESSION_LOCK = threading.RLock()
if not SESSION_DATABASE:
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE accounts (id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE sessions (token_digest TEXT PRIMARY KEY, account_id TEXT, expires_at DATETIME, revoked_at DATETIME)"))
        for name in ("pilot-a", "pilot-b"):
            connection.execute(text("INSERT INTO accounts VALUES (:name)"), {"name": name})

COOKIE = "city_runner_pilot"
UPSTREAM = "http://superset:8088"
ACTIVE = {"pilot-a": 0, "pilot-b": 0}
METADATA_ACTIVE = {"pilot-a": 0, "pilot-b": 0}
CONTROL_ACTIVE = {"pilot-a": 0, "pilot-b": 0}
WAITING = {"pilot-a": 0, "pilot-b": 0}
DEADLINES = {"pilot-a": {}, "pilot-b": {}}
BLOCKED = set()
REQUEST_CAP = 1024 * 1024
RESPONSE_CAP = 8 * 1024 * 1024


def owner(token):
    with SESSION_LOCK, engine.connect() as connection:
        account = account_for_token(connection, token) if token else None
        return account if account in ACTIVE else None


@app.get("/pilot/", response_class=HTMLResponse)
def start():
    return """<!doctype html><title>City Runner synthetic analytics pilot</title>
    <h1>Synthetic analytics pilot</h1><p>Disposable fixtures. No production data.</p>
    <form method="post" action="/pilot/login/pilot-a"><button>Continue as A</button></form>
    <form method="post" action="/pilot/login/pilot-b"><button>Continue as B</button></form>
    <form method="post" action="/pilot/logout"><button>Revoke session</button></form>"""


def check_origin(request):
    if request.headers.get("origin") not in (None, "http://127.0.0.1:8090"):
        raise HTTPException(403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403)


@app.post("/pilot/login/{name}")
def login(name: str, request: Request, expired: bool = False):
    import hashlib

    check_origin(request)
    if name not in {"pilot-a", "pilot-b"}:
        raise HTTPException(404)
    if APP_URL:
        try:
            issued = httpx.post(f'{APP_URL}/fixture/session/{name}', params={'expired': expired}, timeout=2)
            issued.raise_for_status()
            token = issued.json()['token']
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, 'Synthetic application login unavailable.')
        return login_response(request, token)
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK, engine.begin() as connection:
        connection.execute(text("INSERT INTO sessions VALUES (:digest, :owner, :expires, NULL)"), {
            "digest": hashlib.sha256(token.encode()).hexdigest(), "owner": name,
            "expires": utcnow() + (timedelta() if expired else timedelta(minutes=30)),
        })
    return login_response(request, token)


@app.post('/pilot/handoff')
def handoff(request: Request):
    check_origin(request)
    scheme, _, token = request.headers.get('authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not token or len(token) > 512 or owner(token) is None:
        raise HTTPException(401)
    return login_response(request, token)


def login_response(request, token):
    with SESSION_LOCK, engine.begin() as connection:
        old_token = request.cookies.get(COOKIE)
        if old_token and old_token != token:
            revoke_token(connection, old_token)
    response = Response(status_code=303, headers={"Location": "/superset/welcome/", 'Cache-Control': 'no-store'})
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=1800)
    response.delete_cookie("session")
    return response


@app.get('/pilot/app/me')
@app.post('/pilot/app/logout')
def application_session(request: Request):
    """Exercise ordinary app session routes through the internal fixture only."""
    check_origin(request)
    if not APP_URL:
        raise HTTPException(503)
    headers = {'Authorization': f'Bearer {request.cookies.get(COOKIE, "")}'}
    try:
        result = (httpx.get(f'{APP_URL}/api/me', headers=headers, timeout=2)
                  if request.method == 'GET' else
                  httpx.delete(f'{APP_URL}/api/auth/session', headers=headers, timeout=2))
    except httpx.HTTPError:
        raise HTTPException(503, 'Synthetic application unavailable.')
    return Response(result.content, status_code=result.status_code,
                    headers={'Content-Type': 'application/json', 'Cache-Control': 'no-store'})


@app.get("/logout")
@app.get("/logout/")
@app.post("/pilot/logout")
def logout(request: Request):
    check_origin(request)
    with SESSION_LOCK, engine.begin() as connection:
        revoke_token(connection, request.cookies.get(COOKIE, ""))
    response = Response(status_code=303, headers={"Location": "/pilot/"})
    response.delete_cookie(COOKIE)
    response.delete_cookie("session")
    return response


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    started = time.time()
    token = request.cookies.get(COOKIE)
    account = owner(token)
    if account is None:
        return Response(status_code=401, headers={"Cache-Control": "no-store"})
    if account in BLOCKED or any(deadline <= time.time() for deadline in DEADLINES[account].values()):
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    check_origin(request)
    asset = request.method in {"GET", "HEAD"} and path.startswith("static/") and "%" not in path and ".." not in path.split("/")
    # Only explicitly metadata-only GET routes bypass query admission. Unknown
    # routes (including downloads, samples and previews) retain the query budget.
    metadata = request.method in {"GET", "HEAD"} and re.fullmatch(
        r"api/v1/(?:chart|dashboard|saved_query|query)/(?:\d+/?|_info/?|favorite_status/?|related/[^/]+/?)?"
        r"|api/v1/dashboard/\d+/(?:datasets|charts)/?"
        r"|api/v1/(?:me|database|security/csrf_token)/?"
        r"|api/v1/explore/(?:form_data/[^/]+/?)?"
        r"|(?:explore|sqllab|superset/welcome)/?"
        r"|superset/dashboard/\d+/?"
        r"|(?:chart/(?:list|add)|dashboard/list|tablemodelview/list)/?",
        path,
    ) is not None
    metadata = metadata or request.method in {"POST", "PUT", "PATCH", "DELETE"} and re.fullmatch(
        r"api/v1/(?:chart|dashboard|saved_query)/(?:\d+/?)?|api/v1/explore/form_data/?", path,
    ) is not None
    metadata = metadata or re.fullmatch(r'(?:tabstateview|tableschemaview)(?:/[^/]+){0,3}/?', path) is not None
    control = request.method == 'POST' and path.rstrip('/') == 'api/v1/query/stop'
    counters = CONTROL_ACTIVE if control else METADATA_ACTIVE if metadata else ACTIVE
    per_owner, global_cap = (1, 2) if control else (16, 32) if metadata else (2, 4)
    # Superset fans out chart previews and database introspection. Let a bounded
    # burst wait briefly for the same two query slots; SQL Lab remains fail-fast.
    queueable = path.rstrip('/') == 'api/v1/chart/data' or (
        request.method == 'POST' and re.fullmatch(r'api/v1/database/\d+/validate_sql/?', path) is not None) or (
        request.method == 'GET' and path.startswith('api/v1/database/'))
    if not asset and not metadata and not control and queueable and (
        counters[account] >= per_owner or sum(counters.values()) >= global_cap
    ):
        if WAITING[account] >= 8 or sum(WAITING.values()) >= 16:
            return Response(status_code=429, headers={'Cache-Control': 'no-store'})
        WAITING[account] += 1
        try:
            until = time.monotonic() + 1
            while counters[account] >= per_owner or sum(counters.values()) >= global_cap:
                if time.monotonic() >= until:
                    return Response(status_code=429, headers={'Cache-Control': 'no-store'})
                await asyncio.sleep(0.01)
        finally:
            WAITING[account] -= 1
        if owner(token) != account:
            return Response(status_code=401, headers={'Cache-Control': 'no-store'})
        if account in BLOCKED or any(deadline <= time.time() for deadline in DEADLINES[account].values()):
            return Response(status_code=503, headers={'Cache-Control': 'no-store'})
    if not asset and (counters[account] >= per_owner or sum(counters.values()) >= global_cap):
        return Response(status_code=429, headers={"Cache-Control": "no-store"})
    if not asset:
        counters[account] += 1
        request_id = secrets.token_hex(16)
        DEADLINES[account][request_id] = started + 8
    try:
        async with asyncio.timeout(max(0, min(10, started + 10 - time.time()))):
            return await upstream(path, request, token, account, started, asset)
    except TimeoutError:
        if not asset:
            BLOCKED.add(account)
        return Response(status_code=504, headers={"Cache-Control": "no-store"})
    finally:
        if not asset:
            counters[account] -= 1
            DEADLINES[account].pop(request_id)


async def upstream(path, request, token, account, started, asset):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > REQUEST_CAP:
            return Response(status_code=413)
        body.extend(chunk)
    headers = {key: value for key, value in request.headers.items() if not key.lower().startswith(("x-forwarded-", "x-city-runner-")) and key.lower() not in {
        "host", "connection", "authorization", "x-city-runner-account", "remote-user",
        "forwarded", "cookie", "accept-encoding",
    }}
    cookies = SimpleCookie()
    cookies.load(request.headers.get("cookie", ""))
    if "session" in cookies:
        headers["Cookie"] = f"session={cookies['session'].coded_value}"
    headers["X-City-Runner-Account"] = account
    headers["X-City-Runner-Deadline"] = str(started + 8)
    headers["X-Forwarded-Host"] = "127.0.0.1:8090"
    headers["X-Forwarded-Proto"] = "http"
    headers["Accept-Encoding"] = "identity"
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        try:
            async with client.stream(
                request.method, f"{UPSTREAM}/{path}", params=request.query_params,
                content=bytes(body), headers=headers,
            ) as result:
                if result.headers.get("X-Analytics-Cancellation-Failed") == "1":
                    BLOCKED.add(account)
                    return Response(status_code=503, headers={"Cache-Control": "no-store"})
                content = bytearray()
                async for chunk in result.aiter_bytes():
                    if len(content) + len(chunk) > (4 * RESPONSE_CAP if asset else RESPONSE_CAP):
                        if not asset:
                            BLOCKED.add(account)
                        return Response(status_code=502)
                    content.extend(chunk)
        except httpx.RequestError:
            if not asset:
                BLOCKED.add(account)
            return Response(status_code=502, headers={"Cache-Control": "no-store"})
    # An expired/revoked/replaced session cannot publish a completed response.
    if owner(token) != account:
        return Response(status_code=401, headers={"Cache-Control": "no-store"})
    if result.headers.get("X-Analytics-Cancellation-Failed") == "1":
        BLOCKED.add(account)
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    response = Response(bytes(content), status_code=result.status_code)
    for key, value in result.headers.multi_items():
        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "cache-control"}:
            response.headers.append(key, value)
    response.headers["Cache-Control"] = "no-store"
    return response
