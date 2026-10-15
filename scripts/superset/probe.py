"""Run the D43 security gate against disposable fixtures; stdlib only."""

import argparse
import http.cookiejar
import json
import subprocess
import shutil
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path


BASE = "http://127.0.0.1:8090"
COMPOSE = Path(__file__).with_name("compose.yaml")
DOCKER = ["rtk", "docker"] if shutil.which("rtk") else ["docker"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Client:
    def __init__(self, name):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies), NoRedirect(),
        )
        self.csrf = None
        assert self.request("POST", f"/pilot/login/{name}")[0] == 303
        code, body = self.request("GET", "/api/v1/security/csrf_token/")
        assert code == 200, (code, body)
        self.csrf = body["result"]

    def request(self, method, path, data=None, extra_headers=None):
        headers = {"Origin": BASE}
        if self.csrf:
            headers["X-CSRFToken"] = self.csrf
        if data is not None and not isinstance(data, bytes):
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        request = urllib.request.Request(
            BASE + path, data=data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else (b"" if method == "POST" else None),
            headers=headers, method=method,
        )
        try:
            response = self.opener.open(request, timeout=25)
        except urllib.error.HTTPError as error:
            response = error
        raw = response.read()
        if raw.startswith(b"PK"):
            return response.code, raw
        body = raw.decode(errors="replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return response.code, body

    def execute(self, database_id, sql):
        return self.request("POST", "/api/v1/sqllab/execute/", {
            "database_id": database_id, "schema": "analytics", "sql": sql,
            "runAsync": False, "client_id": uuid.uuid4().hex[:10],
            "sql_editor_id": str(uuid.uuid4()), "expand_data": True,
        })

    def form(self, method, path, data):
        return self.request(method, path, urllib.parse.urlencode({key: json.dumps(value) for key, value in data.items()}).encode(),
                            {'Content-Type': 'application/x-www-form-urlencoded'})


def database_query(suffix, sql):
    start = time.monotonic()
    result = subprocess.run([
        *DOCKER, "compose", "-f", str(COMPOSE), "exec", "-T",
        "-e", f"PGPASSWORD=disposable-{suffix}", "postgres", "psql", "-X",
        "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", f"pilot_{suffix}",
        "-d", "analytics", "-Atc", sql,
    ], capture_output=True, text=True, timeout=20)
    return result.returncode, result.stdout.strip(), round(time.monotonic() - start, 3)


def probe(report):
    def check(name, passed, detail=None):
        report["checks"][name] = bool(passed)
        if detail is not None:
            report["observations"][name] = detail
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)

    def accepted(response):
        code, body = response
        assert 200 <= code < 300, (code, body)
        return body

    for suffix, expected in (("a", "1,2"), ("b", "3")):
        code, body, seconds = database_query(suffix,
            "SELECT string_agg(activity_id::text, ',' ORDER BY activity_id) FROM analytics.activities")
        check(f"database_owner_{suffix}", code == 0 and body == expected, {"seconds": seconds})
        for label, sql in {
            "other_role": f"SET ROLE pilot_{'b' if suffix == 'a' else 'a'}",
            "superuser_role": "SET ROLE postgres",
            "other_identity": "SET SESSION AUTHORIZATION postgres",
            "excluded_fields": "SELECT * FROM excluded.credentials",
            "write": "INSERT INTO analytics.activities VALUES ('pilot-a', 4, NULL, NULL, NULL, NULL)",
            "create": "CREATE TABLE public.unwanted (id int)",
            "privileged_function": "SELECT pg_read_file('/etc/passwd')",
        }.items():
            check(f"database_denies_{label}_{suffix}", database_query(suffix, sql)[0] != 0)
        code, body, _ = database_query(suffix,
            "SELECT set_config('city_runner.account_id', 'pilot-b', false); "
            "SELECT string_agg(activity_id::text, ',' ORDER BY activity_id) FROM analytics.activities")
        check(f"mutable_setting_cannot_change_owner_{suffix}", code == 0 and body.splitlines()[-1] == expected)
        code, body, _ = database_query(suffix, "SELECT set_config('statement_timeout', '0', false)")
        check(f"postgres_timeout_is_mutable_{suffix}", code == 0 and body == "0", {"returned": body})

    clients = {name: Client(f"pilot-{name}") for name in ("a", "b")}
    objects = {}
    for name, client in clients.items():
        databases = accepted(client.request("GET", "/api/v1/database/"))["result"]
        check(f"private_connection_catalog_{name}", len(databases) == 1 and databases[0]["database_name"] == f"pilot-{name}")
        database = next(item for item in databases if item["database_name"] == f"pilot-{name}")
        datasets = accepted(client.request("GET", "/api/v1/dataset/"))["result"]
        check(f"private_dataset_catalog_{name}", bool(datasets) and all(item["database"]["id"] == database["id"] for item in datasets))
        dataset = next(item for item in datasets if item["database"]["id"] == database["id"] and item["table_name"] == "activities")
        code, result = client.execute(database["id"], "SELECT * FROM analytics.activities ORDER BY activity_id")
        check(f"sql_lab_owner_{name}", code == 200 and {row["account_id"] for row in result.get("data", [])} == {f"pilot-{name}"}, {"status": code})
        marker = f"{name}-private-{uuid.uuid4().hex[:8]}"
        async_id = uuid.uuid4().hex[:10]
        code, _ = client.request('POST', '/api/v1/sqllab/execute/', {
            'database_id': database['id'], 'schema': 'analytics',
            'sql': f"SELECT '{marker}'", 'runAsync': True, 'client_id': async_id,
            'sql_editor_id': str(uuid.uuid4()),
        })
        check(f'async_execution_rejected_{name}', code == 403)
        check(f'async_result_not_created_{name}', client.request('GET', f'/api/v1/sqllab/export/{async_id}/')[0] in (403, 404))
        unpublished = subprocess.run([
            *DOCKER, 'compose', '-f', str(COMPOSE), 'exec', '-T', 'postgres',
            'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'superset_meta', '-Atc',
            f"SELECT count(*) FROM query WHERE client_id='{async_id}'",
        ], capture_output=True, text=True, check=True, timeout=3)
        check(f'async_job_not_published_{name}', unpublished.stdout.strip() == '0')
        private_result = accepted(client.execute(database["id"], f"SELECT '{marker}' AS private_result"))
        query = accepted(client.request("POST", "/api/v1/saved_query/", {
            "db_id": database["id"], "label": marker, "schema": "analytics",
            "sql": "SELECT SUM(distance_m) FROM analytics.activities",
        }))
        params = {"viz_type": "table", "query_mode": "raw", "all_columns": ["activity_id", "distance_m"], "row_limit": 10000}
        chart = accepted(client.request("POST", "/api/v1/chart/", {
            "slice_name": marker, "datasource_id": dataset["id"], "datasource_type": "table",
            "viz_type": "table", "params": json.dumps(params),
        }))
        dashboard = accepted(client.request("POST", "/api/v1/dashboard/", {
            "dashboard_title": marker, "published": False,
            "position_json": json.dumps({
                "DASHBOARD_VERSION_KEY": "v2",
                "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
                "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": ["ROW-1"], "parents": ["ROOT_ID"]},
                "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": marker}},
                "ROW-1": {"id": "ROW-1", "type": "ROW", "children": ["CHART-1"], "parents": ["ROOT_ID", "GRID_ID"], "meta": {"background": "BACKGROUND_TRANSPARENT"}},
                "CHART-1": {"id": "CHART-1", "type": "CHART", "children": [], "parents": ["ROOT_ID", "GRID_ID", "ROW-1"], "meta": {"chartId": chart["id"], "height": 50, "width": 12, "sliceName": marker}},
            }),
        }))
        objects[name] = {"database": database["id"], "dataset": dataset["id"], "saved_query": query["id"], "chart": chart["id"], "dashboard": dashboard["id"], "marker": marker, "query_id": private_result["query_id"], "client_id": private_result["query"]["id"]}
        tab = accepted(client.form('POST', '/tabstateview/', {'queryEditor': {'dbId': database['id'], 'name': marker, 'sql': f"SELECT '{marker}'", 'remoteId': query['id']}}))
        objects[name]['tab'] = tab['id']
        check(f'own_editor_state_reopens_{name}', client.request('GET', f"/tabstateview/{tab['id']}")[0] == 200)
        schema = accepted(client.form('POST', '/tableschemaview/', {'table': {'queryEditorId': tab['id'], 'dbId': database['id'], 'schema': 'analytics', 'name': 'activities'}}))
        objects[name]['table_schema'] = schema['id']
        virtual = accepted(client.request("POST", "/api/v1/dataset/", {
            "database": database["id"], "schema": "analytics", "table_name": marker,
            "sql": "SELECT activity_id, distance_m FROM analytics.activities",
        }))
        check(f"create_reopen_virtual_dataset_{name}", client.request("GET", f"/api/v1/dataset/{virtual['id']}")[0] == 200)
        accepted(client.request("DELETE", f"/api/v1/dataset/{virtual['id']}"))
        for resource in ("saved_query", "chart", "dashboard"):
            check(f"create_reopen_{resource}_{name}", client.request("GET", f"/api/v1/{resource}/{objects[name][resource]}")[0] == 200)
            check(f"own_{resource}_export_{name}", client.request("GET", f"/api/v1/{resource}/export/?q=!({objects[name][resource]})")[0] == 200)
        code, download = client.request("GET", f"/api/v1/sqllab/export/{objects[name]['client_id']}/")
        check(f"own_result_download_{name}", code == 200 and marker in download)
        for resource in ("sqllab", "explore"):
            path = "/sqllab/" if resource == "sqllab" else f"/explore/?slice_id={chart['id']}"
            check(f"ui_shell_{resource}_{name}", client.request("GET", path)[0] == 200)

    for name, client in clients.items():
        other = objects["b" if name == "a" else "a"]
        current = objects[name]
        check(f'foreign_editor_state_denied_{name}', client.request('GET', f"/tabstateview/{other['tab']}")[0] in (403, 404))
        check(f'editor_ownership_transfer_denied_{name}', client.form('PUT', f"/tabstateview/{current['tab']}", {'user_id': 999})[0] == 403)
        check(f'editor_database_rebinding_denied_{name}', client.form('PUT', f"/tabstateview/{current['tab']}", {'database_id': other['database']})[0] == 403)
        check(f'editor_foreign_saved_query_denied_{name}', client.form('PUT', f"/tabstateview/{current['tab']}", {'saved_query_id': other['saved_query']})[0] in (403, 404))
        check(f'editor_foreign_result_reference_denied_{name}', client.form('PUT', f"/tabstateview/{current['tab']}", {'latest_query_id': other['client_id']})[0] in (403, 404))
        check(f'editor_foreign_query_migration_denied_{name}', client.form('POST', f"/tabstateview/{current['tab']}/migrate_query", {'queryId': other['client_id']})[0] in (403, 404))
        check(f'foreign_table_schema_mutation_denied_{name}', client.form('POST', f"/tableschemaview/{other['table_schema']}/expanded", {'expanded': False})[0] in (403, 404))
        check(f'foreign_table_schema_deletion_denied_{name}', client.request('DELETE', f"/tableschemaview/{other['table_schema']}")[0] in (403, 404))
        check(f'foreign_tab_schema_creation_denied_{name}', client.form('POST', '/tableschemaview/', {'table': {'queryEditorId': other['tab'], 'dbId': current['database'], 'schema': 'analytics', 'name': 'activities'}})[0] in (403, 404))
        code, body = client.execute(other["database"], "SELECT * FROM analytics.activities")
        denied = code in (403, 404, 422) or (code == 500 and body.get("errors", [{}])[0].get("message", "").startswith("The database referenced in this query was not found."))
        check(f"other_connection_execution_denied_{name}", denied, {"status": code})
        for resource in ("dataset", "saved_query", "chart", "dashboard"):
            code, body = client.request("GET", f"/api/v1/{resource}/{other[resource]}")
            check(f"other_{resource}_detail_denied_{name}", code in (403, 404), {"status": code})
            code, body = client.request("GET", f"/api/v1/{resource}/")
            check(f"other_{resource}_list_private_{name}", code == 200 and other["marker"] not in json.dumps(body))
        for resource in ("chart", "saved_query", "dashboard"):
            code, body = client.request("GET", f"/api/v1/{resource}/export/?q=!({other[resource]})")
            check(f"other_{resource}_export_denied_{name}", code in (403, 404), {"status": code})
        code, body = client.request("GET", f"/api/v1/query/{other['query_id']}")
        check(f"other_query_history_detail_denied_{name}", code in (403, 404))
        code, body = client.request("GET", "/api/v1/query/")
        check(f"other_query_history_list_private_{name}", code == 200 and other["marker"] not in json.dumps(body))
        code, body = client.request("GET", f"/api/v1/sqllab/export/{other['client_id']}/")
        check(f"other_result_download_denied_{name}", code in (403, 404), {"status": code, "other_value_returned": other["marker"] in str(body)})
        code, body = client.request("GET", "/api/v1/database/", extra_headers={"X-City-Runner-Account": "pilot-b" if name == "a" else "pilot-a"})
        check(f"forged_identity_ignored_{name}", code == 200 and len(body["result"]) == 1 and body["result"][0]["database_name"] == f"pilot-{name}")
        for resource, payload in {
            "chart": {"slice_name": "unauthorized-change"},
            "saved_query": {"label": "unauthorized-change"},
            "dashboard": {"dashboard_title": "unauthorized-change"},
        }.items():
            code, _ = client.request("PUT", f"/api/v1/{resource}/{other[resource]}", payload)
            check(f"other_{resource}_mutation_denied_{name}", code in (403, 404))
        code, _ = client.request("POST", "/api/v1/chart/", {
            "slice_name": "must-not-be-shared", "datasource_id": other["dataset"],
            "datasource_type": "table", "viz_type": "table", "params": "{}",
        })
        check(f"foreign_datasource_chart_creation_denied_{name}", code == 403)
        code, _ = client.request("PUT", f"/api/v1/chart/{objects[name]['chart']}", {
            "datasource_id": other["dataset"], "datasource_type": "table",
        })
        check(f"chart_datasource_rebinding_denied_{name}", code == 403)
        code, virtual = client.request("POST", "/api/v1/dataset/", {
            "database": objects[name]["database"], "schema": "analytics", "table_name": uuid.uuid4().hex,
            "sql": "SELECT activity_id FROM analytics.activities",
        })
        assert code == 201, (code, virtual)
        code, _ = client.request("PUT", f"/api/v1/dataset/{virtual['id']}", {"database_id": other["database"]})
        check(f"dataset_database_rebinding_denied_{name}", code == 403)
        client.request("DELETE", f"/api/v1/dataset/{virtual['id']}")
        code, _ = client.request("PUT", f"/api/v1/dashboard/{objects[name]['dashboard']}", {"published": True})
        check(f"public_publishing_denied_{name}", code == 403)
        code, _ = client.request("PUT", f"/api/v1/chart/{other['chart']}", {
            "query_context": json.dumps({"datasource": {"id": objects[name]["dataset"], "type": "table"}, "queries": []}),
            "query_context_generation": True,
        })
        check(f"foreign_chart_query_context_mutation_denied_{name}", code in (403, 404))
        for resource in ("chart", "dashboard", "dataset"):
            check(f"metadata_import_disabled_{resource}_{name}", client.request("POST", f"/api/v1/{resource}/import/", {})[0] == 403)

    client = clients["a"]
    stolen_cookies = list(client.cookies)
    client.request("POST", "/pilot/login/pilot-b")
    code, body = client.request("GET", "/api/v1/database/")
    check("account_replacement_changes_identity", code == 200 and [row["database_name"] for row in body["result"]] == ["pilot-b"])
    for cookie in stolen_cookies:
        client.cookies.set_cookie(cookie)
    check("account_replacement_revokes_old_cookie", client.request("GET", "/api/v1/database/")[0] == 401)
    client.request("POST", "/pilot/login/pilot-a?expired=true")
    check("expired_session_denied", client.request("GET", "/api/v1/database/")[0] == 401)

    client = Client("pilot-a")
    token = next(cookie.value for cookie in client.cookies if cookie.name == 'city_runner_pilot')
    check('actual_application_session_valid', client.request('GET', '/pilot/app/me') == (200, {'id': 'pilot-a'}))
    check('existing_bearer_session_handoff', client.request('POST', '/pilot/handoff', extra_headers={'Authorization': f'Bearer {token}'})[0] == 303)
    check('invalid_bearer_handoff_denied', client.request('POST', '/pilot/handoff', extra_headers={'Authorization': 'Bearer invalid'})[0] == 401)
    check('invalid_handoff_preserves_session', client.request('GET', '/api/v1/database/')[0] == 200)
    check('ordinary_application_logout', client.request('POST', '/pilot/app/logout')[0] == 204)
    check('application_logout_revokes_superset', client.request('GET', '/api/v1/database/')[0] == 401)
    check('revoked_bearer_handoff_denied', client.request('POST', '/pilot/handoff', extra_headers={'Authorization': f'Bearer {token}'})[0] == 401)

    client = Client("pilot-a")
    stolen_cookies = list(client.cookies)
    client.request("GET", "/logout/")
    for cookie in stolen_cookies:
        client.cookies.set_cookie(cookie)
    check("logout_revokes_copied_cookie", client.request("GET", "/api/v1/database/")[0] == 401)
    report["objects"] = objects
    report["checks"]["probe_completed"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"version": "6.1.0", "checks": {}, "observations": {}}
    try:
        probe(report)
    except Exception as error:
        report["checks"]["probe_completed"] = False
        report["observations"]["probe_error"] = f"{type(error).__name__}: {error}"
        print(report["observations"]["probe_error"])
    report["not_covered_by_this_probe"] = [
        "query bounds and database termination: run bounds_probe.py separately",
        "interactive browser authoring and reviewed production dataset contract",
        "representative workload capacity and hosted CI",
    ]
    report["probe_passed"] = all(report["checks"].values())
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Authoring/privacy probes: {'PASS' if report['probe_passed'] else 'FAIL'}; full D43 acceptance is recorded separately.")
    return 0 if report["probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
