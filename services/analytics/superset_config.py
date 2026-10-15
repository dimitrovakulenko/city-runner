"""Pinned Superset configuration for the City Runner authoring service."""

import importlib.util
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

if os.environ.get("ANALYTICS_MODE") != "production":
    raise RuntimeError("The application analytics configuration requires production mode.")

pilot = Path(__file__).resolve().parents[2] / "scripts/superset/superset_config.py"
spec = importlib.util.spec_from_file_location("city_runner_superset_base", pilot)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
globals().update({name: value for name, value in vars(base).items() if name.isupper()})

from security import PrivateQuerySecurityManager
from query_boundary import connection_mutator

CUSTOM_SECURITY_MANAGER = PrivateQuerySecurityManager
DB_CONNECTION_MUTATOR = connection_mutator
SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
if len(SECRET_KEY.encode()) < 32 or len(os.environ["ANALYTICS_SECRET"].encode()) < 32:
    raise ValueError("Analytics secrets require at least 32 bytes.")
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_METADATA_DATABASE_URL"]
SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"connect_timeout": 1,
    "options": "-c statement_timeout=1500 -c lock_timeout=250"}}
SESSION_COOKIE_SECURE = urlsplit(os.environ["ANALYTICS_PUBLIC_ORIGIN"]).scheme == "https"
SESSION_COOKIE_SAMESITE = "Strict"
TALISMAN_CONFIG = {**TALISMAN_CONFIG, "force_https": SESSION_COOKIE_SECURE,
                   "session_cookie_secure": SESSION_COOKIE_SECURE}
SCARF_ANALYTICS = False


class SafeFailures(logging.Filter):
    def filter(self, record):
        # Database errors can contain SQL literals/private values. Keep useful
        # INFO query IDs; suppress payloads and tracebacks on authoring failures.
        if record.name.startswith(("superset", "sqlalchemy")) and record.levelno >= logging.WARNING:
            record.msg, record.args = "Analytics operation failed.", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def configure(app):
    from services.analytics.superset_rpc import register_rpc

    register_rpc(app)
    base.configure_identity(app, valid_owner=lambda value: re.fullmatch(r"[0-9a-f]{64}", value or "") is not None,
                            internal_endpoint="analytics_internal")
    handlers = set(logging.getLogger().handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers.update(logger.handlers)
    for handler in handlers:
        handler.addFilter(SafeFailures())


FLASK_APP_MUTATOR = configure
