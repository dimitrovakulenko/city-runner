"""Upload, export, and delete one disposable account through the real APIs."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import time
import urllib.request
import uuid
import zipfile

from application_probe import API, ApplicationClient, application, ready


def run(fixture, report, before_delete=None):
    account = fixture["lifecycle"]
    boundary = uuid.uuid4().hex
    gpx = b'<?xml version="1.0"?><gpx version="1.1" creator="synthetic-analytics-acceptance"><trk><name>Synthetic lifecycle</name><type>running</type><trkseg><trkpt lat="51.002" lon="3.710"><time>2026-10-01T10:00:00Z</time></trkpt><trkpt lat="51.002" lon="3.7104"><time>2026-10-01T10:01:00Z</time></trkpt></trkseg></trk></gpx>'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic.gpx"\r\nContent-Type: application/gpx+xml\r\n\r\n'.encode()
        + gpx + f'\r\n--{boundary}--\r\n'.encode())
    request = urllib.request.Request(API + "/api/uploads", data=body, headers={
        "Authorization": "Bearer " + account["token"], "Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(request, timeout=15) as response:
        source = json.load(response)
        assert response.code == 202
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        code, status = application(account, "GET", "/api/uploads/" + source["id"])
        assert code == 200
        if status["status"] == "succeeded":
            break
        assert status["status"] != "failed", status
        time.sleep(0.5)
    report["checks"]["normal_upload_succeeded"] = status["status"] == "succeeded"
    assert application(account, "PUT", "/api/analytics/preferences", {"dataset_id": fixture["dataset_id"]})[0] == 204
    state = ready(account)
    client = ApplicationClient(account)
    code, catalog = client.request("GET", "/api/v1/database/")
    assert code == 200
    database = catalog["result"][0]["id"]
    code, result = client.execute(database, "SELECT count(*) AS activities FROM analytics.activities")
    report["checks"]["uploaded_activity_projected"] = code == 200 and result["data"][0]["activities"] == 1
    code, query = client.request("POST", "/api/v1/saved_query/", {
        "db_id": database, "label": "Synthetic lifecycle definition", "schema": "analytics", "sql": "SELECT 1"})
    assert code == 201
    marker = "lifecycle-authoring-" + hashlib.sha256(account["id"].encode()).hexdigest()
    code, cached = client.request("POST", f"/api/v1/dashboard/{state['boards'][0]['id']}/filter_state", {"value": json.dumps({"private_marker": marker})})
    assert code == 201, (code, cached)
    report["checks"]["private_filter_state_created"] = marker in str(client.request("GET", f"/api/v1/dashboard/{state['boards'][0]['id']}/filter_state/{cached['key']}")[1])
    code, datasets = client.request("GET", "/api/v1/dataset/")
    assert code == 200
    dataset = next(row["id"] for row in datasets["result"] if row["table_name"] == "activities")
    code, cached = client.request("POST", "/api/v1/explore/form_data", {"datasource_id": dataset, "datasource_type": "table", "form_data": json.dumps({"private_marker": marker})})
    assert code == 201, (code, cached)
    report["checks"]["private_explore_state_created"] = marker in str(client.request("GET", f"/api/v1/explore/form_data/{cached['key']}")[1])
    code, archive = application(account, "GET", "/api/account/export")
    assert code == 200
    with zipfile.ZipFile(io.BytesIO(archive)) as exported:
        report["checks"]["private_definition_exported"] = "Synthetic lifecycle definition" in exported.read("data/analytics.json").decode()
        report["checks"]["original_exported"] = any(name.endswith(".gpx") for name in exported.namelist())
    if before_delete:
        before_delete()
    code, receipt = application(account, "DELETE", "/api/account", {"confirmation": "DELETE"})
    report["checks"]["durable_cleanup_pending"] = code == 202 and receipt["status"] == "cleanup-pending"
    report["checks"]["app_session_revoked"] = application(account, "GET", "/api/me")[0] == 401
    report["checks"]["analytics_session_revoked"] = client.request("GET", "/api/v1/dashboard/" + state["boards"][0]["id"])[0] == 401
    report["checks"]["stale_query_denied"] = client.request("GET", f"/api/v1/saved_query/{query['id']}")[0] == 401
    report["checks"]["other_account_unaffected"] = application(fixture["accounts"]["B"], "GET", "/api/analytics")[0] == 200


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=Path("/tmp/city-runner-analytics-acceptance.json"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/d45-lifecycle.json"))
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
