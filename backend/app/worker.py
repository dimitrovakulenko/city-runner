"""Small worker process for durable jobs."""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from backend.app.jobs import claim, complete, fail

logger = logging.getLogger(__name__)
Handler = Callable[[dict[str, Any]], None]


def _noop(job: dict[str, Any]) -> None:
    logger.info("completed synthetic dev.noop job")


def run_once(engine: Engine, handlers: dict[str, Handler], *, lease_seconds: int = 60) -> bool:
    if not handlers:
        return False
    job = claim(engine, lease_seconds=lease_seconds, kind=tuple(handlers))
    if job is None:
        return False
    handler = handlers[job["kind"]]
    try:
        handler(job)
    except Exception as error:
        error_code = re.sub(r"[^a-z0-9_]", "_", type(error).__name__.lower())[:64]
        if not error_code or not error_code[0].isalpha():
            error_code = "handler_error"
        fail(engine, job_id=job["id"], lease_token=job["lease_token"], error_code=error_code)
        logger.error("job %s failed (%s)", job["id"], error_code)
    else:
        complete(engine, job_id=job["id"], lease_token=job["lease_token"])
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run queued City Runner jobs")
    parser.add_argument("--once", action="store_true", help="claim at most one synthetic dev.noop job")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    engine = create_engine(os.getenv("DATABASE_URL", "postgresql+psycopg://localhost/activities"), pool_pre_ping=True)
    try:
        if args.once:
            run_once(engine, {"dev.noop": _noop})
            return
        while True:
            if not run_once(engine, {"dev.noop": _noop}):
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
