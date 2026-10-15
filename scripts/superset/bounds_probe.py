"""Verify driver deadlines, database termination, cancellation and gateway caps."""

import argparse
import concurrent.futures
import json
import subprocess
import time
import uuid
import statistics
from pathlib import Path

from probe import Client, COMPOSE, DOCKER, database_query


def admin(sql):
    result = subprocess.run([
        *DOCKER, "compose", "-f", str(COMPOSE), "exec", "-T", "postgres",
        "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "analytics", "-Atc", sql,
    ], capture_output=True, text=True, check=True, timeout=3)
    return result.stdout.strip()


def active(owner="pilot_a"):
    assert owner in {"pilot_a", "pilot_b"}
    return int(admin(f"SELECT count(*) FROM pg_stat_activity WHERE datname='analytics' AND usename='{owner}' AND state='active' AND query LIKE '%pilot_slow%';"))


def execute(client, sql, client_id=None):
    return client.request("POST", "/api/v1/sqllab/execute/", {
        "database_id": 1, "schema": "analytics", "sql": sql, "runAsync": False,
        "client_id": client_id or uuid.uuid4().hex[:10], "sql_editor_id": str(uuid.uuid4()),
    })


def cancelled(response):
    code, body = response
    # Stock Superset can observe STOPPED before handling the driver error and
    # return HTTP 200 with no results, instead of its usual HTTP 500 error.
    return code >= 400 or (code == 200 and isinstance(body, dict)
                           and body.get('status') == 'stopped' and not body.get('data'))


def run(report):
    def check(name, passed, detail=None):
        report["checks"][name] = bool(passed)
        if detail is not None:
            report["observations"][name] = detail
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)

    def timed(name, call):
        started = time.monotonic()
        code, body = call()
        seconds = round(time.monotonic() - started, 3)
        gone = active() == 0
        check(name, code >= 400 and 7 <= seconds < 10 and gone, {"status": code, "seconds": seconds, "database_query_gone": gone})
        return code, body

    client = Client("pilot-a")
    for setting in (
        "SELECT set_config('statement_timeout','0',false)",
        "SET statement_timeout=0",
        "RESET statement_timeout; SELECT set_config('statement_timeout','0',false)",
    ):
        client_id = uuid.uuid4().hex[:10]
        timed(f"sql_lab_external_deadline_{setting.split()[0].lower()}", lambda: execute(
            client, setting + "; SELECT analytics.pilot_slow(15)", client_id,
        ))
        check("queries_continue_after_successful_termination", execute(client, "SELECT 1")[0] == 200)

    # Re-download a recorded slow query: exports re-execute independently of SQL Lab.
    timed("download_external_deadline", lambda: client.request("GET", f"/api/v1/sqllab/export/{client_id}/"))

    admin("CREATE OR REPLACE FUNCTION analytics.pilot_slow_value(seconds double precision) RETURNS integer LANGUAGE sql AS $$ SELECT 1 FROM pg_sleep(seconds) $$")
    code, virtual = client.request("POST", "/api/v1/dataset/", {
        "database": 1, "schema": "analytics", "table_name": f"slow_{uuid.uuid4().hex[:8]}",
        "sql": "SELECT activity_id, analytics.pilot_slow_value(0) AS delay FROM analytics.activities LIMIT 1",
    })
    assert code == 201, (code, virtual)
    admin("CREATE OR REPLACE FUNCTION analytics.pilot_slow_value(seconds double precision) RETURNS integer LANGUAGE sql AS $$ SELECT 1 FROM pg_sleep(CASE WHEN seconds=0 THEN 15 ELSE seconds END) $$")
    try:
        timed("explore_external_deadline", lambda: client.request("POST", "/api/v1/chart/data", {
            "datasource": {"id": virtual["id"], "type": "table"}, "force": True,
            "queries": [{"columns": ["activity_id", "delay"], "metrics": [], "row_limit": 10, "filters": [], "orderby": [], "extras": {}, "time_range": "No filter"}],
            "result_format": "json", "result_type": "full",
        }))
    finally:
        admin("CREATE OR REPLACE FUNCTION analytics.pilot_slow_value(seconds double precision) RETURNS integer LANGUAGE sql AS $$ SELECT 1 FROM pg_sleep(seconds) $$")
    client.request("DELETE", f"/api/v1/dataset/{virtual['id']}")

    code, rows = execute(client, "SELECT generate_series(1,10000) AS n")
    check("ten_thousand_rows_allowed", code == 200 and len(rows.get("data", [])) == 10000)
    code, rows = execute(client, "SELECT generate_series(1,10001) AS n")
    check("row_overflow_explicitly_rejected", code >= 400 and "10000 rows" in str(rows))
    check("request_size_bound", execute(client, " " * (1024 * 1024))[0] == 413)

    clients = [Client("pilot-a") for _ in range(3)]
    other = Client("pilot-b")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(execute, c, "SELECT analytics.pilot_slow(3)") for c in clients[:2]]
        deadline = time.monotonic() + 2
        while active() < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        count = active()
        check("two_owner_queries_running", count == 2, {"active": count})
        check("third_owner_request_rejected", execute(clients[2], "SELECT 1")[0] == 429)
        check("other_account_remains_available", other.execute(2, "SELECT 1")[0] == 200)
        check("admitted_queries_finish", all(job.result()[0] == 200 for job in jobs))

    running = Client("pilot-a")
    control = Client("pilot-a")
    client_id = uuid.uuid4().hex[:10]
    second_id = uuid.uuid4().hex[:10]
    second = Client('pilot-a')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        job = pool.submit(execute, running, "SELECT analytics.pilot_slow(15)", client_id)
        second_job = pool.submit(execute, second, 'SELECT analytics.pilot_slow(15)', second_id)
        deadline = time.monotonic() + 2
        while active() < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        check('two_queries_running_before_cancel', active() == 2)
        check('third_sql_rejected_during_cancel', execute(control, 'SELECT 1')[0] == 429)
        code, _ = other.request("POST", "/api/v1/query/stop", {"client_id": client_id})
        check("other_account_cannot_cancel_query", code in (403, 404) and active() == 2)
        started = time.monotonic()
        code, _ = control.request("POST", "/api/v1/query/stop", {"client_id": client_id})
        result = job.result()
        seconds = round(time.monotonic() - started, 3)
        check("explicit_cancel_terminates_database_query", code == 200 and cancelled(result) and active() == 1 and seconds < 2, {"seconds": seconds, "status": code, "execution_status": result[0]})
        check('cancel_preserves_other_running_query', not second_job.done() and active() == 1)
        code, _ = control.request('POST', '/api/v1/query/stop', {'client_id': second_id})
        second_result = second_job.result()
        remaining = active()
        check('second_cancel_terminates_remaining_query', code == 200 and cancelled(second_result) and remaining == 0,
              {'status': code, 'execution_status': second_result[0], 'remaining_queries': remaining})

    running = Client("pilot-a")
    # Revocation during an admitted request must withhold the completed result.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(execute, running, "SELECT analytics.pilot_slow(2)")
        deadline = time.monotonic() + 1
        while active() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        revoke = Client("pilot-a")
        revoke.cookies = running.cookies
        revoke.opener = running.opener
        revoke.request("POST", "/pilot/logout")
        check("logout_fences_inflight_result", job.result()[0] == 401)

    check("cancel_role_has_no_analytics_data_access", admin("SELECT has_table_privilege('pilot_cancel','analytics.activities','SELECT')") == "f")
    check("owners_cannot_call_cancel_control", admin("SELECT has_schema_privilege('pilot_a','control','USAGE') OR has_function_privilege('pilot_a','control.terminate_query(text,integer,timestamptz)','EXECUTE')") == "f")
    check("response_size_bound", execute(client, "SELECT repeat('x',8388609) AS too_large")[0] == 502)
    check("discarded_upstream_response_fences_owner", execute(client, "SELECT 1")[0] == 503)
    report["checks"]["probe_completed"] = True


def failure_case(report):
    client = Client("pilot-b")
    other = Client("pilot-a")
    sql = "SELECT set_config('statement_timeout','0',false); SELECT analytics.pilot_slow(15)"
    admin("REVOKE EXECUTE ON FUNCTION control.terminate_query(text,integer,timestamptz) FROM pilot_cancel")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            started = time.monotonic()
            job = pool.submit(client.execute, 2, sql)
            deadline = started + 2
            while active("pilot_b") == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert active("pilot_b") == 1
            while time.monotonic() - started < 8.3:
                time.sleep(0.05)
            report["checks"]["failed_cancellation_fences_new_admission"] = client.request("GET", "/api/v1/database/")[0] == 503 and active("pilot_b") == 1
            # Independent operator recovery of only this synthetic owner's query.
            admin("SELECT pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE datname='analytics' AND usename='pilot_b' AND state='active' AND query LIKE '%pilot_slow%'")
            report["checks"]["failed_cancellation_withholds_inflight_result"] = job.result()[0] == 503
            report["checks"]["failure_fence_survives_response_cleanup"] = client.request("GET", "/api/v1/database/")[0] == 503
            report["checks"]["failure_is_owner_scoped"] = other.execute(1, "SELECT 1")[0] == 200
            report["observations"]["fault_duration_seconds"] = round(time.monotonic() - started, 3)
    finally:
        admin("GRANT EXECUTE ON FUNCTION control.terminate_query(text,integer,timestamptz) TO pilot_cancel")
        admin("SELECT pg_terminate_backend(pid,500) FROM pg_stat_activity WHERE datname='analytics' AND usename='pilot_b' AND state='active' AND query LIKE '%pilot_slow%'")
    report["checks"]["probe_completed"] = True
    for name, passed in report["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)


def provisioning_case(report):
    output = admin(Path(__file__).with_name("provisioning.sql").read_text())
    report["observations"]["role_policy_provisioning"] = json.loads(next(line for line in output.splitlines() if line.startswith("{")))
    samples = []
    for _ in range(10):
        code, body, _ = database_query("a", "SELECT extract(epoch FROM clock_timestamp()-backend_start)*1000 FROM pg_stat_activity WHERE pid=pg_backend_pid()")
        assert code == 0, body
        samples.append(float(body))
    report["observations"]["authenticated_connection_start_ms"] = {
        "samples": len(samples), "mean": statistics.mean(samples), "max": max(samples),
    }
    report["checks"]["provisioning_rolled_back"] = admin("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'pilot_bench_%'") == "0"
    report["checks"]["probe_completed"] = True
    print(json.dumps(report["observations"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--failure-only", action="store_true")
    parser.add_argument("--provision-only", action="store_true")
    args = parser.parse_args()
    report = {"checks": {}, "observations": {}}
    try:
        (provisioning_case if args.provision_only else failure_case if args.failure_only else run)(report)
    except Exception as error:
        report["checks"]["probe_completed"] = False
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"])
    report["probe_passed"] = all(report["checks"].values())
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
