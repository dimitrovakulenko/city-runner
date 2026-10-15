"""Shared full-UI proxy with bounded SQL, metadata and cancellation admission."""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from http.cookies import SimpleCookie
import re
import logging
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

REQUEST_CAP = 1024 * 1024
RESPONSE_CAP = 8 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    owner: str
    version: str = ''


class AuthoringProxy:
    def __init__(self, *, resolve, fence, check_origin, cookie, upstream, public_origin,
                 admit=None, finish=None):
        self.resolve, self.fence, self.check_origin = resolve, fence, check_origin
        self.cookie, self.url = cookie, upstream.rstrip('/')
        self.admit, self.finish = admit, finish
        origin = urlsplit(public_origin)
        self.host, self.scheme = origin.netloc, origin.scheme
        self.active, self.metadata_active, self.control_active, self.waiting = (defaultdict(int) for _ in range(4))
        self.deadlines, self.blocked = {}, set()

    async def block(self, owner, reason="boundary_failed"):
        logger.warning("Analytics boundary fenced an owner (%s).", reason)
        self.blocked.add(owner)
        try:
            await asyncio.wait_for(self.fence(owner), timeout=1)
        except Exception:
            # A failed durable fence retains the local fence. Startup must
            # reconcile live role connections before accepting any traffic.
            pass

    async def proxy(self, path: str, request: Request):
        if path.split('/')[0] == 'integration':
            return Response(status_code=404)
        started = time.time()
        token = request.cookies.get(self.cookie)
        identity = await self.resolve(token)
        account = identity.owner if identity else None
        if account is None:
            return Response(status_code=401, headers={"Cache-Control": "no-store"})
        if account in self.blocked or any(deadline <= time.time() for deadline in self.deadlines.get(account, {}).values()):
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        self.check_origin(request)
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
        counters = self.control_active if control else self.metadata_active if metadata else self.active
        per_owner, global_cap = (1, 2) if control else (16, 32) if metadata else (2, 4)
        # Superset fans out chart previews and database introspection. Let a bounded
        # burst wait briefly for the same two query slots; SQL Lab remains fail-fast.
        queueable = path.rstrip('/') == 'api/v1/chart/data' or (
            request.method == 'POST' and re.fullmatch(r'api/v1/database/\d+/validate_sql/?', path) is not None) or (
            request.method == 'GET' and path.startswith('api/v1/database/'))
        if not asset and not metadata and not control and queueable and (
            counters.get(account, 0) >= per_owner or sum(counters.values()) >= global_cap
        ):
            if self.waiting.get(account, 0) >= 8 or sum(self.waiting.values()) >= 16:
                return Response(status_code=429, headers={'Cache-Control': 'no-store'})
            self.waiting[account] += 1
            try:
                until = time.monotonic() + 1
                while counters.get(account, 0) >= per_owner or sum(counters.values()) >= global_cap:
                    if time.monotonic() >= until:
                        return Response(status_code=429, headers={'Cache-Control': 'no-store'})
                    await asyncio.sleep(0.01)
            finally:
                self.waiting[account] -= 1
                if not self.waiting[account]:
                    self.waiting.pop(account)
            if await self.resolve(token) != identity:
                return Response(status_code=401, headers={'Cache-Control': 'no-store'})
            if account in self.blocked or any(deadline <= time.time() for deadline in self.deadlines.get(account, {}).values()):
                return Response(status_code=503, headers={'Cache-Control': 'no-store'})
        if not asset and (counters.get(account, 0) >= per_owner or sum(counters.values()) >= global_cap):
            return Response(status_code=429, headers={"Cache-Control": "no-store"})
        if not asset:
            counters[account] += 1
            request_id = secrets.token_hex(16)
            self.deadlines.setdefault(account, {})[request_id] = started + 8
        try:
            # Reserve one second for persisting a failure fence within ten total.
            async with asyncio.timeout(max(0, min(9, started + 9 - time.time()))):
                if not asset and self.admit:
                    await self.admit(identity, token, request_id)
                response = await self.upstream(path, request, token, identity, started, asset)
                if not asset and self.finish and account not in self.blocked:
                    await self.finish(identity, request_id)
                return response
        except TimeoutError:
            if not asset:
                await self.block(account, "outer_deadline")
            return Response(status_code=504, headers={"Cache-Control": "no-store"})
        except HTTPException as error:
            if error.status_code in (401, 409):
                return Response(status_code=error.status_code, headers={"Cache-Control": "no-store"})
            if not asset:
                await self.block(account, "admission_or_authentication_failed")
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        except Exception:
            if not asset:
                await self.block(account, "unexpected_boundary_failure")
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        finally:
            if not asset:
                counters[account] -= 1
                self.deadlines[account].pop(request_id)
                if not self.deadlines[account]:
                    self.deadlines.pop(account)
                if not counters[account]:
                    counters.pop(account)


    async def upstream(self, path, request, token, identity, started, asset):
        account = identity.owner
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > REQUEST_CAP:
                return Response(status_code=413)
            body.extend(chunk)
        headers = {key: value for key, value in request.headers.items() if not key.lower().startswith(("x-forwarded-", "x-city-runner-")) and key.lower() not in {
            "host", "connection", "authorization", "x-city-runner-account", "remote-user",
            "forwarded", "cookie", "accept-encoding", "x-analytics-secret", "proxy-authorization", "x-remote-user",
        }}
        cookies = SimpleCookie()
        cookies.load(request.headers.get("cookie", ""))
        if "session" in cookies:
            headers["Cookie"] = f"session={cookies['session'].coded_value}"
        headers["X-City-Runner-Account"] = account
        headers["X-City-Runner-Deadline"] = str(started + 8)
        headers["X-Forwarded-Host"] = self.host
        headers["X-Forwarded-Proto"] = self.scheme
        headers["Accept-Encoding"] = "identity"
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            try:
                async with client.stream(
                    request.method, f"{self.url}/{path}", params=request.query_params,
                    content=bytes(body), headers=headers,
                ) as result:
                    if result.headers.get("X-Analytics-Cancellation-Failed") == "1":
                        await self.block(account, "cancellation_failed")
                        return Response(status_code=503, headers={"Cache-Control": "no-store"})
                    content = bytearray()
                    async for chunk in result.aiter_bytes():
                        if len(content) + len(chunk) > (4 * RESPONSE_CAP if asset else RESPONSE_CAP):
                            if not asset:
                                await self.block(account, "response_limit")
                            return Response(status_code=502)
                        content.extend(chunk)
            except httpx.RequestError:
                if not asset:
                    await self.block(account, "upstream_transport")
                return Response(status_code=502, headers={"Cache-Control": "no-store"})
        # An expired/revoked/replaced session cannot publish a completed response.
        if await self.resolve(token) != identity:
            return Response(status_code=401, headers={"Cache-Control": "no-store"})
        if result.headers.get("X-Analytics-Cancellation-Failed") == "1":
            await self.block(account)
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        response = Response(bytes(content), status_code=result.status_code)
        for key, value in result.headers.multi_items():
            if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection", "cache-control"}:
                response.headers.append(key, value)
        response.headers["Cache-Control"] = "no-store"
        return response
