"""Disposable synchronous PostgreSQL pilot boundary, pinned to psycopg2."""

import threading
import time
import os
from contextvars import ContextVar

import psycopg2
from psycopg2.extensions import connection, cursor
from sqlalchemy.pool import NullPool
from services.analytics.identity import role_name, cancel_role, cancel_password


OWNERS = {"pilot-a": "pilot_a", "pilot-b": "pilot_b"}
ROW_CAP = 10000
FAILED_OWNERS = set()
REQUEST_SCOPE = ContextVar("pilot_analytics_request", default=None)


def owner_role(owner):
    return role_name(owner) if os.getenv("ANALYTICS_MODE") == "production" else OWNERS[owner]


def cancel_parameters():
    if os.getenv("ANALYTICS_MODE") != "production":
        return {"host": "postgres", "dbname": "analytics", "user": "pilot_cancel", "password": "disposable-cancel"}
    from sqlalchemy.engine.url import make_url

    url = make_url(os.environ["ANALYTICS_DATABASE_URL"])
    secret = os.environ["ANALYTICS_SECRET"].encode()
    return {"host": url.host, "port": url.port or 5432, "dbname": url.database,
            "user": cancel_role(url.database), "password": cancel_password(secret, url.database)}


class BoundedCursor(cursor):
    def execute(self, *args, **kwargs):
        self.connection.check_request()
        self._read_rows = 0
        return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        raise psycopg2.OperationalError("Analytics batch writes are disabled.")

    def _rows(self, rows):
        self._read_rows = getattr(self, "_read_rows", 0) + len(rows)
        if self._read_rows > ROW_CAP:
            raise psycopg2.OperationalError("Analytics result exceeds 10000 rows; narrow the query.")
        return rows

    def fetchmany(self, size=None):
        self.connection.check_request()
        remaining = ROW_CAP + 1 - getattr(self, "_read_rows", 0)
        return self._rows(super().fetchmany(min(size or self.arraysize, remaining)))

    def fetchall(self):
        return self.fetchmany(ROW_CAP + 1)

    def fetchone(self):
        self.connection.check_request()
        row = super().fetchone()
        if row is not None:
            self._rows([row])
        return row

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def copy_expert(self, *args, **kwargs):
        raise psycopg2.OperationalError("Analytics COPY is disabled.")

    copy_from = copy_expert
    copy_to = copy_expert
    callproc = copy_expert


class BoundedConnection(connection):
    def __init__(self, *args, **kwargs):
        scope = REQUEST_SCOPE.get()
        if scope is None:
            raise psycopg2.OperationalError("Analytics requires an authenticated HTTP request.")
        self._owner, self._deadline = scope
        self._expired = False
        self._kill_verified = False
        self.check_request()
        super().__init__(*args, **kwargs)
        if self.get_dsn_parameters().get("user") != owner_role(self._owner):
            super().close()
            raise psycopg2.OperationalError("Analytics connection owner mismatch.")
        # Fixed control query only; every caller-created cursor is bounded below.
        with super().cursor(cursor_factory=cursor) as control:
            control.execute("SELECT backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()")
            self._backend_start = control.fetchone()[0]
        self._pid = self.get_backend_pid()
        self._timer = threading.Timer(max(0, self._deadline - time.monotonic()), self._terminate)
        self._timer.daemon = True
        self._timer.start()

    def check_request(self):
        if self._expired or time.monotonic() >= self._deadline:
            raise psycopg2.OperationalError("Analytics request deadline exceeded.")
        scope = REQUEST_SCOPE.get()
        if scope is None or scope[0] != self._owner:
            raise psycopg2.OperationalError("Analytics request owner mismatch.")

    def cursor(self, *args, **kwargs):
        self.check_request()
        if args or kwargs.get("name"):
            raise psycopg2.OperationalError("Named analytics cursors are disabled.")
        kwargs["cursor_factory"] = BoundedCursor
        return super().cursor(**kwargs)

    def _terminate(self):
        self._expired = True
        try:
            with psycopg2.connect(
                **cancel_parameters(), connect_timeout=1,
                options="-c statement_timeout=1000",
            ) as control:
                with control.cursor() as command:
                    command.execute("SELECT control.terminate_query(%s, %s, %s)", (
                        self._owner, self._pid, self._backend_start,
                    ))
                    self._kill_verified = command.fetchone()[0]
        except psycopg2.Error:
            self._kill_verified = False
        if not self._kill_verified:
            FAILED_OWNERS.add(self._owner)

    def close(self):
        timer = getattr(self, "_timer", None)
        if timer:
            timer.cancel()
            timer.join()
        super().close()


def connection_mutator(url, parameters, username, security_manager, source):
    scope = REQUEST_SCOPE.get()
    database = cancel_parameters()["dbname"]
    if scope is None or url.database != database or url.username != owner_role(scope[0]):
        raise psycopg2.OperationalError("Only owner-bound pilot connections are allowed.")
    parameters = dict(parameters)
    for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        parameters.pop(key, None)
    parameters["poolclass"] = NullPool
    parameters["connect_args"] = {
        "connection_factory": BoundedConnection, "connect_timeout": 1,
    }
    return url, parameters
