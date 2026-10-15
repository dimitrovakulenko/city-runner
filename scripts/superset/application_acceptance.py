"""Disposable real-app acceptance; never opens an existing application DB."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ["rtk", "docker"] if shutil.which("rtk") else ["docker"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=8011)
    parser.add_argument("--ui-port", type=int, default=8091)
    parser.add_argument("--web-port", type=int, default=4183)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--capacity", action="store_true")
    parser.add_argument("--recovery", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    admin_url = make_url(os.environ["SUPERSET_PROBE_ADMIN_DATABASE_URL"])
    if not admin_url.password or admin_url.host not in {"127.0.0.1", "localhost"}:
        parser.error("An explicitly configured loopback disposable PostgreSQL admin URL is required.")
    suffix = uuid.uuid4().hex[:12]
    database = "city_runner_analytics_acceptance_" + suffix
    project = "city-runner-analytics-acceptance-" + suffix
    admin = create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as db:
        db.exec_driver_sql("CREATE DATABASE " + database)
    web = None
    with tempfile.TemporaryDirectory(prefix="city-runner-analytics-acceptance-") as temporary:
        root = Path(temporary)
        env_file = root / "services.env"
        env_file.write_text("\n".join(f"{key}='{value}'" for key, value in {
            "DATABASE_URL": admin_url.set(database=database, host="host.docker.internal").render_as_string(hide_password=False),
            **{key: secrets.token_hex(32) for key in ("ANALYTICS_SECRET", "SUPERSET_SECRET_KEY", "ANALYTICS_POSTGRES_PASSWORD", "SUPERSET_METADATA_PASSWORD")},
            "ANALYTICS_PUBLIC_ORIGIN": f"http://127.0.0.1:{args.ui_port}", "WEB_ORIGIN": f"http://127.0.0.1:{args.web_port}",
            "ANALYTICS_LOCAL_API_PORT": str(args.api_port), "ANALYTICS_LOCAL_PORT": str(args.ui_port),
        }.items()) + "\n")
        env_file.chmod(0o600)
        compose = [*DOCKER, "compose", "-p", project, "--env-file", str(env_file),
            "-f", str(ROOT / "services/analytics/compose.yaml"), "-f", str(ROOT / "scripts/superset/acceptance-compose.yaml")]
        env = {**os.environ, "ANALYTICS_ACCEPTANCE_API": f"http://127.0.0.1:{args.api_port}",
            "ANALYTICS_ACCEPTANCE_UI": f"http://127.0.0.1:{args.ui_port}", "ANALYTICS_ACCEPTANCE_WEB": f"http://127.0.0.1:{args.web_port}"}
        fixture = root / "accounts.json"
        def run(command, **kwargs):
            kwargs.setdefault("env", env)
            return subprocess.run(command, check=True, cwd=ROOT, **kwargs)
        def metadata_probe(filename):
            with (args.output / filename).open("w") as log:
                run([*compose, "exec", "-T", "-e", "CITY_RUNNER_SYNTHETIC_ACCEPTANCE=1", "superset", "python", "-"],
                    input=(ROOT / "scripts/superset/metadata_lifecycle_probe.py").read_text(), text=True, stdout=log, stderr=subprocess.STDOUT)
        try:
            run([*compose, "up", "-d", "--build", "--wait", "--wait-timeout", "180", "app", "superset", "gateway"])
            run([*compose, "exec", "-T", "app", "python", "scripts/superset/acceptance_seed.py"])
            run([*compose, "up", "-d", "--wait", "projector", "imports"])
            run([*compose, "cp", "app:/var/lib/city-runner/acceptance.json", str(fixture)])
            fixture.chmod(0o600)
            run([sys.executable, "scripts/superset/application_probe.py", "--fixture", str(fixture), "--output", str(args.output / "application.json")])
            if args.capacity:
                run([*compose, "exec", "-T", "app", "python", "scripts/superset/capacity_seed.py"])
                run([*compose, "cp", "app:/var/lib/city-runner/acceptance.json", str(fixture)])
                run([sys.executable, "scripts/superset/capacity_probe.py", "--fixture", str(fixture), "--output", str(args.output / "capacity.json")])
                resources = {}
                for service in ("app", "projector", "imports", "superset", "gateway", "analytics-postgres"):
                    peak = run([*compose, "exec", "-T", service, "cat", "/sys/fs/cgroup/memory.peak"], capture_output=True, text=True)
                    resources[service] = {"peak_memory_bytes": int(peak.stdout.strip())}
                (args.output / "resources.json").write_text(json.dumps(resources, indent=2) + "\n")
            if not args.no_browser:
                log = (args.output / "web.log").open("w")
                web = subprocess.Popen(["npm", "run", "preview", "--", "--port", str(args.web_port)],
                    cwd=ROOT / "apps/web", env={**env, "API_PROXY_TARGET": env["ANALYTICS_ACCEPTANCE_API"]}, stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        urllib.request.urlopen(env["ANALYTICS_ACCEPTANCE_WEB"], timeout=1).close()
                        break
                    except OSError:
                        if time.monotonic() > deadline or web.poll() is not None:
                            raise RuntimeError("Production web preview did not start.")
                        time.sleep(0.2)
                run(["node", "scripts/superset/application_browser_probe.mjs", "--fixture", str(fixture), "--output", str(args.output / "browser.json")])
            run([*compose, "exec", "-T", "app", "python", "scripts/superset/lifecycle_fixture.py", "create"])
            run([*compose, "cp", "app:/var/lib/city-runner/acceptance.json", str(fixture)])
            recovery = None
            if args.recovery:
                os.environ.update({key: value for key, value in env.items() if key.startswith("ANALYTICS_ACCEPTANCE_")})
                # Imported here so each standalone run uses its isolated ports.
                from restart_acceptance import verify
                verify(run=run, compose=compose, fixture=json.loads(fixture.read_text()), output=args.output / "restart.json")
                from recovery_acceptance import Recovery
                recovery = Recovery(run=run, docker=DOCKER, compose=compose, admin=admin, source_url=admin_url,
                    database=database, directory=root, output=args.output, env=env)
                os.environ.update({key: value for key, value in env.items() if key.startswith("ANALYTICS_ACCEPTANCE_")})
                from lifecycle_probe import run as lifecycle
                report = {"checks": {"completed": False}, "failures": []}
                try:
                    lifecycle(json.loads(fixture.read_text()), report, before_delete=recovery.backup)
                    report["checks"]["completed"] = True
                finally:
                    report["probe_passed"] = all(report["checks"].values())
                    (args.output / "lifecycle.json").write_text(json.dumps(report, indent=2) + "\n")
                if not report["probe_passed"]:
                    raise RuntimeError("Application lifecycle acceptance failed.")
            else:
                run([sys.executable, "scripts/superset/lifecycle_probe.py", "--fixture", str(fixture), "--output", str(args.output / "lifecycle.json")])
            deadline = time.monotonic() + 60
            while True:
                status = run([*compose, "exec", "-T", "app", "python", "scripts/superset/lifecycle_fixture.py", "status"], capture_output=True, text=True)
                state = json.loads(status.stdout)
                if state["receipt"] == "complete" and state["analytics"]["state"] == "deleted":
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Durable analytics cleanup did not complete.")
                time.sleep(0.5)
            metadata_probe("metadata-cleanup.log")
            run([*compose, "stop", "projector", "imports"])
            run([*compose, "exec", "-T", "app", "python", "-m", "backend.app.account_data", "--restore"])
            if recovery:
                recovery.restore_and_verify()
                metadata_probe("metadata-restored-cleanup.log")
            (args.output / "completion.json").write_text(json.dumps({"application": True, "browser": not args.no_browser,
                "durable_cleanup": True, "metadata_orphan_cleanup": True, "journal_replay_verified": True}) + "\n")
        finally:
            if web:
                web.terminate(); web.wait(timeout=10)
            with (args.output / "containers.log").open("w") as log:
                subprocess.run([*compose, "logs", "--no-color"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            subprocess.run([*compose, "down", "-v"], cwd=ROOT, check=True)
            with admin.connect() as db:
                db.exec_driver_sql("DROP DATABASE " + database + " WITH (FORCE)")
            admin.dispose()


if __name__ == "__main__":
    main()
