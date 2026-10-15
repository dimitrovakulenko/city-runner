"""Mixed bounded SQL, maps and an accepted import against synthetic owners."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import time
import urllib.request
import uuid

from application_probe import API, ApplicationClient, application, ready


def statistics(values):
    values = sorted(values)
    return {"samples": len(values), "p50_ms": round(values[math.ceil(len(values) * .5) - 1], 2),
        "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 2), "max_ms": round(max(values), 2)}


def run(fixture, report):
    heavy, other = fixture["capacity"], fixture["accounts"]["B"]
    assert application(heavy, "PUT", "/api/analytics/preferences", {"dataset_id": fixture["dataset_id"]})[0] == 204
    ready(heavy)
    clients = [ApplicationClient(account, "sql") for account in (heavy, other)]
    databases = [client.request("GET", "/api/v1/database/")[1]["result"][0]["id"] for client in clients]

    def sql(index):
        samples = []
        for _ in range(5):
            started = time.monotonic()
            code, result = clients[index].execute(databases[index], "SELECT activity_type,count(*) AS activities,sum(distance_m) AS known_distance_m FROM analytics.activities GROUP BY activity_type")
            assert code == 200, ("Bounded mixed SQL request failed", code, result.get("message"), result.get("errors"))
            assert sum(row["activities"] for row in result["data"]) == (5000 if index == 0 else 2)
            samples.append((time.monotonic() - started) * 1000)
        return samples

    def maps():
        samples = []
        for _ in range(10):
            started = time.monotonic()
            code, result = application(other, "GET", "/api/map?bbox=3.709,51.001,3.715,51.004&zoom=17")
            assert code == 200 and result["streets"], ("Mixed map request failed", code)
            samples.append((time.monotonic() - started) * 1000)
        return samples

    def upload():
        account = fixture["accounts"]["A"]
        marker = uuid.uuid4().hex
        content = f'<gpx version="1.1" creator="synthetic-capacity"><trk><name>Mixed import {marker}</name><trkseg><trkpt lat="51.002" lon="3.710"><time>2026-10-15T10:00:00Z</time></trkpt><trkpt lat="51.002" lon="3.7104"><time>2026-10-15T10:01:00Z</time></trkpt></trkseg></trk></gpx>'.encode()
        body = (f'--{marker}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-capacity.gpx"\r\nContent-Type: application/gpx+xml\r\n\r\n'.encode()
            + content + f'\r\n--{marker}--\r\n'.encode())
        started = time.monotonic()
        request = urllib.request.Request(API + "/api/uploads", data=body, headers={"Authorization": "Bearer " + account["token"],
            "Content-Type": "multipart/form-data; boundary=" + marker})
        with urllib.request.urlopen(request, timeout=15) as response:
            assert response.code == 202
            source = json.load(response)
        while time.monotonic() - started < 60:
            code, state = application(account, "GET", "/api/uploads/" + source["id"])
            assert code == 200 and state["status"] != "failed"
            if state["status"] == "succeeded":
                return (time.monotonic() - started) * 1000
            time.sleep(0.1)
        raise RuntimeError("The accepted import was starved during analytics load.")

    with ThreadPoolExecutor(7) as pool:
        queries = [pool.submit(sql, index) for index in (0, 0, 1, 1)]
        map_reads = [pool.submit(maps) for _ in range(2)]
        imported = pool.submit(upload)
        query_times = sum((future.result() for future in queries), [])
        map_times = sum((future.result() for future in map_reads), [])
        import_ms = imported.result()
    ready(fixture["accounts"]["A"])
    code, result = clients[0].execute(databases[0], "SELECT count(DISTINCT node_token) AS supported FROM analytics.activity_nodes")
    report["checks"]["five_thousand_activities_not_truncated"] = True
    report["checks"]["five_million_source_samples_project_to_scalar_support"] = code == 200 and result["data"][0]["supported"] == 2000
    report["checks"]["four_concurrent_owner_scoped_sql_requests"] = len(query_times) == 20 and max(query_times) < 10_000
    report["checks"]["maps_remain_available"] = len(map_times) == 20
    report["checks"]["accepted_import_completes"] = import_ms < 60_000
    report["observations"] = {"retained_activities": 5000, "original_gps_samples": 5_000_000, "analytics_concurrency": 4,
        "map_concurrency": 2, "sql": statistics(query_times), "map": statistics(map_times), "accepted_import_ms": round(import_ms, 2),
        "scope": "Synthetic two-city workload; heavy-owner full account archive size is not a capacity acceptance claim."}
    fixture.setdefault("activity_counts", {"A": 4, "B": 2})["A"] += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    report = {"checks": {}, "failures": []}
    try:
        run(fixture, report)
        report["checks"]["completed"] = True
        args.fixture.write_text(json.dumps(fixture))
    except Exception as error:
        report["checks"]["completed"] = False
        report["failures"].append(str(error))
    report["probe_passed"] = all(report["checks"].values())
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
