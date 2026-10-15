"""D43 localhost synthetic pilot; deliberately not production configuration."""

import os
import sys
import math
import time
import json

from flask import abort, g, request
from flask_appbuilder.security.manager import AUTH_REMOTE_USER
from flask_login import current_user, login_user, logout_user

sys.path.insert(0, "/pilot")
from security import PrivateQuerySecurityManager
from query_boundary import REQUEST_SCOPE
from editor_state import guard_editor_state

if os.environ.get("PILOT_STOCK_SECURITY") != "1":
    CUSTOM_SECURITY_MANAGER = PrivateQuerySecurityManager
    if os.environ.get("PILOT_BOOTSTRAP") != "1":
        from query_boundary import connection_mutator

        DB_CONNECTION_MUTATOR = connection_mutator

SECRET_KEY = "disposable-synthetic-pilot-key-never-use-in-production-2026"
SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://superset_meta:disposable-metadata@postgres/superset_meta"
AUTH_TYPE = AUTH_REMOTE_USER
AUTH_USER_REGISTRATION = False
AUTH_ROLE_PUBLIC = "Public"
ENABLE_PROXY_FIX = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False  # Loopback HTTP only; deployment requires HTTPS.
WTF_CSRF_ENABLED = True
TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "force_https": False,
    "session_cookie_secure": False,
    "content_security_policy": {
        "base-uri": ["'self'"],
        "default-src": ["'self'"],
        "img-src": ["'self'", "blob:", "data:"],
        "worker-src": ["'self'", "blob:"],
        "connect-src": ["'self'"],
        "object-src": ["'none'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "script-src": ["'self'", "'strict-dynamic'"],
        "frame-ancestors": ["'none'"],
    },
    "content_security_policy_nonce_in": ["script-src"],
}
FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": False,
    "EMBEDDED_SUPERSET": False,
    "ALERT_REPORTS": False,
    "GLOBAL_ASYNC_QUERIES": False,
    "DASHBOARD_RBAC": False,
    "ENABLE_SUPERSET_META_DB": False,
    "ENABLE_JAVASCRIPT_CONTROLS": False,
}
SQL_MAX_ROW = 10000
ROW_LIMIT = 10000
SQLLAB_TIMEOUT = 10
SUPERSET_WEBSERVER_TIMEOUT = 15
SQLLAB_ASYNC_TIME_LIMIT_SEC = 10
SQLLAB_CTAS_NO_LIMIT = False
PREVENT_UNSAFE_DB_CONNECTIONS = True
DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
WTF_CSRF_TIME_LIMIT = 3600


def configure_identity(app, *, valid_owner=None, internal_endpoint=None):
    @app.before_request
    def city_runner_identity():
        # Only the gateway is published. It overwrites this header after checking
        # the real City Runner bearer-session validator on every request.
        from superset import security_manager

        if request.path == "/health":
            return None
        if internal_endpoint and request.endpoint == internal_endpoint:
            return None
        name = request.headers.get("X-City-Runner-Account")
        if not (valid_owner(name) if valid_owner else name in {"pilot-a", "pilot-b"}):
            logout_user()
            abort(401)
        user = security_manager.find_user(username=name)
        if user is None or not user.is_active:
            abort(401)
        if not current_user.is_authenticated or current_user.id != user.id:
            logout_user()
            login_user(user, remember=False)
        g.user = user
        try:
            remaining = float(request.headers["X-City-Runner-Deadline"]) - time.time()
            if not math.isfinite(remaining) or remaining <= 0:
                abort(408)
        except (KeyError, ValueError):
            abort(401)
        g.analytics_owner = name
        g.analytics_deadline = time.monotonic() + min(remaining, 8)
        REQUEST_SCOPE.set((name, g.analytics_deadline))
        editor_state = guard_editor_state(user, name)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.path.startswith("/api/v1/") and not editor_state:
            abort(405)
        if set(request.path.split("/")) & {"import", "upload", "embedded", "permalink", "export_streaming"}:
            abort(403)
        if request.method in {"POST", "PUT", "PATCH"}:
            payload = request.get_json(silent=True) or {}
            if isinstance(payload, dict):
                if payload.get("runAsync"):
                    abort(403)
                if any(owner != user.id for owner in payload.get("owners", [])):
                    abort(403)
                if payload.get("published") or payload.get("roles"):
                    abort(403)
                from superset import db
                from superset.connectors.sqla.models import SqlaTable
                from superset.models.core import Database
                from superset.models.dashboard import Dashboard
                from superset.models.slice import Slice

                own_database = db.session.query(Database).filter_by(database_name=name).one()
                for field in ("database", "database_id", "db_id"):
                    if field in payload and payload[field] != own_database.id:
                        abort(403)

                def own_dataset(identifier):
                    dataset = db.session.query(SqlaTable).filter_by(id=identifier, database_id=own_database.id).one_or_none()
                    if dataset is None:
                        abort(403)

                if "datasource_id" in payload:
                    own_dataset(payload["datasource_id"])
                for data in (payload, payload.get("query_context")):
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except ValueError:
                            abort(400)
                    if isinstance(data, dict) and "datasource" in data:
                        reference = data["datasource"]
                        if isinstance(reference, str) and reference.endswith("__table"):
                            reference = {"id": reference[:-7], "type": "table"}
                        if not isinstance(reference, dict) or reference.get("type") != "table":
                            abort(403)
                        own_dataset(reference.get("id"))
                for identifier in payload.get("dashboards", []):
                    dashboard = db.session.query(Dashboard).filter_by(id=identifier).one_or_none()
                    if dashboard is None or user not in dashboard.owners:
                        abort(403)
                if payload.get("position_json"):
                    try:
                        positions = json.loads(payload["position_json"])
                    except (TypeError, ValueError):
                        abort(400)
                    for component in positions.values():
                        if isinstance(component, dict) and component.get("type") == "CHART":
                            chart = db.session.query(Slice).filter_by(id=component.get("meta", {}).get("chartId")).one_or_none()
                            if chart is None or user not in chart.owners:
                                abort(403)
                if request.path.rstrip("/") == "/api/v1/query/stop":
                    from superset.models.sql_lab import Query

                    query = db.session.query(Query).filter_by(client_id=payload.get("client_id"), user_id=user.id).one_or_none()
                    if query is None:
                        abort(404)

    @app.after_request
    def cancellation_status(response):
        from query_boundary import FAILED_OWNERS

        if getattr(g, "analytics_owner", None) in FAILED_OWNERS:
            response.headers["X-Analytics-Cancellation-Failed"] = "1"
        REQUEST_SCOPE.set(None)
        return response


FLASK_APP_MUTATOR = configure_identity
