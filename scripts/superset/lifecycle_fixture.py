"""Operator-only synthetic lifecycle fixture in a generated acceptance database."""

import json
import os
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.auth import create_identity_and_session


def main():
    url = make_url(os.environ["DATABASE_URL"])
    if re.fullmatch(r"city_runner_analytics_acceptance_[0-9a-f]{12}", url.database or "") is None:
        raise RuntimeError("Only a generated disposable acceptance database is permitted.")
    fixture = Path("/var/lib/city-runner/acceptance.json")
    data = json.loads(fixture.read_text())
    engine = create_engine(url)
    if sys.argv[1] == "create":
        with engine.begin() as db:
            identity = create_identity_and_session(db, "google", "synthetic-analytics-lifecycle-" + uuid.uuid4().hex)
        data["lifecycle"] = {"id": identity["account"]["id"], "token": identity["token"]}
        fixture.write_text(json.dumps(data))
        print("Created a synthetic lifecycle account; credentials remain private.")
    elif sys.argv[1] == "status":
        with engine.connect() as db:
            state = db.execute(text("SELECT state,last_error FROM analytics_accounts WHERE account_id=:id"),
                {"id": data["lifecycle"]["id"]}).mappings().first()
            receipt = db.execute(text("SELECT status FROM account_deletions WHERE account_id=:id"),
                {"id": data["lifecycle"]["id"]}).scalar_one_or_none()
        print(json.dumps({"analytics": dict(state) if state else None, "receipt": receipt}))
    else:
        raise RuntimeError("Expected create or status.")
    engine.dispose()


if __name__ == "__main__":
    main()
