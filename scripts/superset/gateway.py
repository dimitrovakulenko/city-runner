"""Loopback-only synthetic City Runner session / full Superset UI pilot."""

import asyncio
import secrets
import time
import threading
import os
from datetime import timedelta

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from services.analytics.proxy import AuthoringProxy, Identity

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
PUBLIC_ORIGIN = os.getenv("PILOT_PUBLIC_ORIGIN", "http://127.0.0.1:8090")
PILOT_ACCOUNTS = {"pilot-a", "pilot-b"}


def owner(token):
    with SESSION_LOCK, engine.connect() as connection:
        account = account_for_token(connection, token) if token else None
        return account if account in PILOT_ACCOUNTS else None


@app.get("/pilot/", response_class=HTMLResponse)
def start():
    return """<!doctype html><title>City Runner synthetic analytics pilot</title>
    <h1>Synthetic analytics pilot</h1><p>Disposable fixtures. No production data.</p>
    <form method="post" action="/pilot/login/pilot-a"><button>Continue as A</button></form>
    <form method="post" action="/pilot/login/pilot-b"><button>Continue as B</button></form>
    <form method="post" action="/pilot/logout"><button>Revoke session</button></form>"""


def check_origin(request):
    if request.headers.get("origin") not in (None, PUBLIC_ORIGIN):
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


async def resolve_session(token):
    account = await asyncio.to_thread(owner, token)
    return Identity(account) if account else None


async def local_fence(account):
    pass  # Disposable pilot recovery is explicitly process-local.


boundary = AuthoringProxy(resolve=resolve_session, fence=local_fence, check_origin=check_origin,
    cookie=COOKIE, upstream=UPSTREAM, public_origin=PUBLIC_ORIGIN)
app.add_api_route("/{path:path}", boundary.proxy,
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
