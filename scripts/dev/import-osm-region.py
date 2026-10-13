#!/usr/bin/env python3
"""Submit, inspect or retry a public OSM XML region import."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from backend.app.map_imports import get_map_task, retry_map_task, submit_osm_import


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="snapshot and queue an operator-supplied public XML extract")
    submit.add_argument("--file", required=True)
    submit.add_argument("--region", required=True)
    submit.add_argument("--city-relation", type=int, action="append", required=True)
    submit.add_argument("--source-timestamp")
    submit.add_argument("--coverage-mode", choices=("complete", "sampled"), default="sampled")
    submit.add_argument("--coverage-evidence", default="")
    for name in ("status", "retry"):
        command = subparsers.add_parser(name)
        command.add_argument("--task-id", type=int, required=True)
        if name == "retry":
            command.add_argument("--source-timestamp")
    args = parser.parse_args()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        if args.command == "submit":
            result = submit_osm_import(engine, args.file, region=args.region,
                city_relation_ids=args.city_relation, source_timestamp=args.source_timestamp,
                coverage_mode=args.coverage_mode, coverage_evidence=args.coverage_evidence)
        elif args.command == "status":
            result = get_map_task(engine, args.task_id)
        else:
            result = retry_map_task(engine, args.task_id, source_timestamp=args.source_timestamp)
        if result is None:
            parser.error("map import task not found")
        print(json.dumps(result, sort_keys=True, default=lambda value: value.isoformat()))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
