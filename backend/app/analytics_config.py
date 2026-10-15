"""Explicit opt-in configuration for the isolated analytics services."""

from dataclasses import dataclass
from functools import lru_cache
import os
from urllib.parse import urlsplit

from sqlalchemy import create_engine


def checked_origins(analytics, web):
    origins = [value.rstrip("/") for value in (analytics, web)]
    parsed = [urlsplit(value) for value in origins]
    for origin in parsed:
        if (origin.path or origin.query or origin.fragment or origin.username or not origin.hostname
                or (origin.scheme != "https" and not (origin.scheme == "http" and origin.hostname in {"127.0.0.1", "localhost"}))):
            raise ValueError("Analytics origins require HTTPS or explicit loopback HTTP.")
    # This first integration supports separate ports on one host. Strict cookies
    # then accompany the handoff, including account-replacement revocation.
    if origins[0] == origins[1] or (parsed[0].scheme, parsed[0].hostname) != (parsed[1].scheme, parsed[1].hostname):
        raise ValueError("Analytics and web require distinct origins on the same scheme and hostname.")
    return origins


@dataclass
class AnalyticsConfig:
    store: object
    secret: bytes
    public_origin: str
    web_origin: str
    superset_internal_url: str

    @classmethod
    def from_env(cls):
        database = os.getenv("ANALYTICS_ADMIN_DATABASE_URL")
        if not database:
            return None
        return cls._configured(database, os.environ["ANALYTICS_SECRET"],
            os.environ["ANALYTICS_PUBLIC_ORIGIN"], os.environ["WEB_ORIGIN"], os.environ["SUPERSET_INTERNAL_URL"])

    @classmethod
    @lru_cache(maxsize=4)
    def _configured(cls, database, secret_value, public_origin, web_origin, superset_url):
        secret = secret_value.encode()
        if len(secret) < 32:
            raise ValueError("ANALYTICS_SECRET must contain at least 32 bytes.")
        origins = checked_origins(public_origin, web_origin)
        store = create_engine(database, pool_pre_ping=True, pool_size=4, max_overflow=0,
                              pool_timeout=1, connect_args={"connect_timeout": 1})
        return cls(store, secret, *origins, superset_url.rstrip("/"))
