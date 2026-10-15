"""Real-query deadline, cancellation and crash recovery in a disposable stack."""

from concurrent.futures import ThreadPoolExecutor
import json
import time
import uuid

from application_probe import ApplicationClient, application, ready


def verify(*, run, compose, fixture, output):
    report = {"checks": {"completed": False}}
    account = fixture.get("capacity", fixture["accounts"]["B"])
    expected = account.get("activities", 2)
    client = ApplicationClient(account, "sql")
    database = client.request("GET", "/api/v1/database/")[1]["result"][0]["id"]

    def operator(mode):
        result = run([*compose, "exec", "-T", "app", "python", "scripts/superset/recovery_fixture.py", mode], capture_output=True, text=True)
        return json.loads(result.stdout)

    def wait(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = operator("activity")
            if predicate(state):
                return state
            time.sleep(.1)
        raise RuntimeError("Analytics database boundary did not reach its expected state.")

    try:
        operator("slow-fixture")
        started = time.monotonic()
        code, body = client.execute(database, "SELECT set_config('statement_timeout','0',false); SELECT analytics.acceptance_slow(30)")
        elapsed = time.monotonic() - started
        report["deadline_observation"] = {"status": code, "seconds": round(elapsed, 3)}
        assert code != 200 or body.get("status") == "failed"
        assert 5 < elapsed < 11 and wait(lambda state: state["active"] == 0)["active"] == 0
        report["checks"]["immutable_deadline_terminates_actual_database_work"] = True
        report["deadline_seconds"] = round(elapsed, 3)
        with ThreadPoolExecutor(1) as pool:
            handle = uuid.uuid4().hex[:10]
            pending = pool.submit(client.request, "POST", "/api/v1/sqllab/execute/", {
                "database_id": database, "schema": "analytics", "sql": "SELECT analytics.acceptance_slow(30)",
                "runAsync": False, "client_id": handle, "sql_editor_id": str(uuid.uuid4()), "expand_data": True})
            wait(lambda state: state["active"] == 1)
            control = ApplicationClient(account, "sql")
            assert operator("activity")["active"] == 1 and not pending.done()
            stopped = time.monotonic()
            assert control.request("POST", "/api/v1/query/stop", {"client_id": handle})[0] == 200
            code, result = pending.result()
            assert code >= 400 or result.get("status") == "stopped" and not result.get("data")
            wait(lambda state: state["active"] == 0)
            assert time.monotonic() - stopped < 3
        report["checks"]["native_stop_terminates_actual_database_work"] = True
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.execute, database, "SELECT analytics.acceptance_slow(6)")
            wait(lambda state: state["active"] == 1 and state["pending"] > 0)
            run([*compose, "kill", "-s", "SIGKILL", "gateway"])
            try:
                pending.result()
            except OSError:
                pass  # The deliberately killed gateway cannot return a response.
        run([*compose, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "90", "gateway"])
        state = wait(lambda state: state["fenced"] and state["active"] == 0)
        assert state["fenced"] and application(account, "GET", "/api/analytics")[1]["state"] != "ready"
        assert client.request("GET", "/api/v1/database/")[0] != 200
        report["checks"]["crash_reconciles_durable_admission_and_withholds_results"] = True
        operator("recover")
        run([*compose, "restart", "gateway"])
        run([*compose, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "60", "gateway"])
        ready(account)
        assert client.execute(database, "SELECT count(*) AS activities FROM analytics.activities")[1]["data"][0]["activities"] == expected
        report["checks"]["operator_recovery_republishes_fresh_complete_projection"] = True
        run([*compose, "restart", "app", "superset", "projector", "imports"])
        run([*compose, "up", "-d", "--no-deps", "--wait", "--wait-timeout", "90", "app", "superset", "projector", "imports"])
        ready(account)
        assert client.execute(database, "SELECT count(*) AS activities FROM analytics.activities")[1]["data"][0]["activities"] == expected
        assert len(application(account, "GET", "/api/analytics")[1]["boards"]) >= 3
        report["checks"]["api_superset_workers_restart_preserves_private_definitions"] = True
        report["checks"]["completed"] = True
    finally:
        report["probe_passed"] = all(report["checks"].values())
        output.write_text(json.dumps(report, indent=2) + "\n")
