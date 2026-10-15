"""Two synthetic accounts through the application-configured analytics stack."""

import argparse
import http.cookiejar
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from probe import Client, NoRedirect

API = os.getenv("ANALYTICS_ACCEPTANCE_API", "http://127.0.0.1:8011")
UI = os.getenv("ANALYTICS_ACCEPTANCE_UI", "http://127.0.0.1:8091")
WEB = os.getenv("ANALYTICS_ACCEPTANCE_WEB", "http://127.0.0.1:4183")


def application(account, method, path, data=None):
    headers = {"Authorization": "Bearer " + account["token"]}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, headers=headers, method=method,
        data=json.dumps(data).encode() if data is not None else None)
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    try:
        body = raw if raw.startswith(b"PK") else json.loads(raw) if raw else None
    except ValueError:
        body = "Non-JSON application error"
    return response.code, body


class ApplicationClient(Client):
    def __init__(self, account, destination="default"):
        self.base = UI
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect())
        self.csrf = None
        code, ticket = application(account, "POST", "/api/analytics/handoff", {"destination": destination})
        assert code == 200, (code, ticket)
        code, _ = self.request("POST", "/session", urllib.parse.urlencode({"ticket": ticket["ticket"]}).encode(), {
            "Origin": WEB, "Content-Type": "application/x-www-form-urlencoded",
        })
        assert code == 303, code
        code, csrf = self.request("GET", "/api/v1/security/csrf_token/")
        assert code == 200, (code, csrf)
        self.csrf = csrf["result"]


def ready(account):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        code, state = application(account, "GET", "/api/analytics")
        if code == 200 and state["state"] == "ready":
            return state
        time.sleep(0.5)
    raise RuntimeError("Synthetic projection did not become ready.")


def run(fixture, report):
    accounts = fixture["accounts"]
    clients, objects = {}, {}
    for label, account in accounts.items():
        assert account["id"].startswith("acceptance-")
        code, _ = application(account, "PUT", "/api/analytics/preferences", {"dataset_id": fixture["dataset_id"]})
        assert code == 204
        state = ready(account)
        report["checks"][f"editable_private_starters_{label}"] = len(state["boards"]) >= 3
        client = clients[label] = ApplicationClient(account)
        code, catalog = client.request("GET", "/api/v1/database/")
        assert code == 200 and len(catalog["result"]) == 1
        database = catalog["result"][0]["id"]
        code, result = client.execute(database, "SELECT count(*) AS activities FROM analytics.activities")
        report["checks"][f"projected_activity_grain_{label}"] = code == 200 and result["data"][0]["activities"] == fixture.get("activity_counts", {"A": 4, "B": 2})[label]
        marker = f"{label}-application-private-{uuid.uuid4().hex[:8]}"
        code, query = client.request("POST", "/api/v1/saved_query/", {
            "db_id": database, "label": marker, "schema": "analytics", "sql": "SELECT count(*) FROM analytics.activities",
        })
        assert code == 201, (code, query)
        code, board = application(account, "POST", "/api/analytics/boards", {"template": "activity"})
        assert code == 201, (code, board)
        report["checks"][f"independent_template_copy_{label}"] = board["id"] not in {item["id"] for item in state["boards"]}
        code, _ = client.request("PUT", f"/api/v1/dashboard/{board['id']}", {"dashboard_title": marker})
        assert code == 200
        code, _ = application(account, "PUT", "/api/analytics/preferences", {"default_dashboard_id": board["id"]})
        report["checks"][f"default_persisted_{label}"] = code == 204 and ready(account)["default_dashboard_id"] == board["id"]
        objects[label] = {"board": board["id"], "query": query["id"], "database": database, "marker": marker}
        code, dataset = client.request("POST", "/api/v1/dataset/", {
            "database": database, "schema": "analytics", "table_name": marker,
            "sql": "SELECT activity_id,distance_m FROM analytics.activities", "owners": [],
        })
        assert code == 201, (code, dataset)
        code, edited = client.request("PUT", f"/api/v1/dataset/{dataset['id']}", {
            "columns": [{"column_name": "authored_km", "expression": "distance_m / 1000.0", "type": "FLOAT"}],
            "metrics": [{"metric_name": "authored_distance", "expression": "SUM(distance_m)", "metric_type": "sum"}],
        })
        assert code == 200, (code, edited)
        code, blank = application(account, "POST", "/api/analytics/boards", {"template": "blank"})
        assert code == 201
        code, blank_detail = client.request("GET", f"/api/v1/dashboard/{blank['id']}")
        assert code == 200
        code, _ = client.request("PUT", f"/api/v1/dashboard/{blank['id']}", {"owners": []})
        assert code == 200
        code, still_owned = client.request("GET", f"/api/v1/dashboard/{blank['id']}")
        report["checks"][f"private_ownership_cannot_be_removed_{label}"] = code == 200 and bool(still_owned["result"]["owners"])
        objects[label]["blank"] = blank["id"]
        objects[label]["blank_uuid"] = blank_detail["result"]["uuid"]
        code, charts = client.request("GET", f"/api/v1/dashboard/{board['id']}/charts")
        assert code == 200
        objects[label]["chart"] = charts["result"][0]["id"]
        code, archive = application(account, "GET", "/api/account/export")
        assert code == 200, (code, archive)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            definitions = exported.read("data/analytics.json").decode()
            report["checks"][f"account_export_includes_private_definitions_{label}"] = marker in definitions
            report["checks"][f"account_export_includes_authored_columns_metrics_{label}"] = all(value in definitions for value in ("authored_km", "distance_m / 1000.0", "authored_distance", "SUM(distance_m)"))
            report["checks"][f"account_export_excludes_credentials_{label}"] = not any(value in definitions for value in (
                "sqlalchemy_uri", "encrypted_password", "app_session_digest", account["token"]))
    for label, client in clients.items():
        other = objects["B" if label == "A" else "A"]
        report["checks"][f"foreign_dashboard_denied_{label}"] = client.request("GET", f"/api/v1/dashboard/{other['board']}")[0] in (403, 404)
        report["checks"][f"foreign_saved_query_denied_{label}"] = client.request("GET", f"/api/v1/saved_query/{other['query']}")[0] in (403, 404)
        report["checks"][f"foreign_execution_denied_{label}"] = client.execute(other["database"], "SELECT 1")[0] in (403, 404)
        report["checks"][f"foreign_default_denied_{label}"] = application(accounts[label], "PUT", "/api/analytics/preferences", {"default_dashboard_id": other["board"]})[0] == 404
        report["checks"][f"foreign_blank_uuid_denied_{label}"] = client.request("GET", f"/api/v1/dashboard/{other['blank_uuid']}")[0] in (403, 404)
        report["checks"][f"foreign_blank_page_denied_{label}"] = client.request("GET", f"/superset/dashboard/{other['blank_uuid']}/")[0] in (403, 404)
        report["checks"][f"foreign_blank_copy_denied_{label}"] = client.request("POST", f"/api/v1/dashboard/{other['blank_uuid']}/copy/", {
            "dashboard_title": "Must not copy foreign board", "json_metadata": "{}", "duplicate_slices": True,
        })[0] in (403, 404)
        for kind in ("CHART", "ROW"):
            report["checks"][f"foreign_nested_{kind.lower()}_placement_denied_{label}"] = client.request("POST", f"/api/v1/dashboard/{objects[label]['blank']}/copy/", {
                "dashboard_title": "Must not attach foreign chart", "duplicate_slices": False,
                "json_metadata": json.dumps({"positions": {"forged": {"type": kind, "meta": {"chartId": other["chart"]}}}}),
            })[0] in (403, 404)
    report["objects"] = objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=Path("/tmp/city-runner-analytics-acceptance.json"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/d45-application.json"))
    args = parser.parse_args()
    report = {"checks": {}, "failures": []}
    try:
        run(json.loads(args.fixture.read_text()), report)
        report["checks"]["completed"] = True
    except Exception as error:
        report["checks"]["completed"] = False
        report["failures"].append(str(error))
    report["probe_passed"] = all(report["checks"].values())
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
