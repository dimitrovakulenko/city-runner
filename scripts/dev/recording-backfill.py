#!/usr/bin/env python3
"""Explicit bounded backfill of exact GPX/FIT recording identities."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from backend.app.recording_backfill import backfill_recording_identities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--after-id", type=int)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-activities", type=int, default=100)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = backfill_recording_identities(
            engine, args.account_id, after_id=args.after_id, batch_size=args.batch_size,
            max_activities=args.max_activities,
        )
        print(json.dumps(result, separators=(",", ":")))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
