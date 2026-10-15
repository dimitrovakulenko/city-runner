"""Host-side backup/restore drill for one disposable application stack."""

import json
from pathlib import Path

POSTGRES = "postgres:16.10@sha256:21f6013073bc6b92830a2129570e2f5ec42a6c734b5a985a41e83aa58f54c3c1"


class Recovery:
    def __init__(self, *, run, docker, compose, admin, source_url, database, directory, output, env):
        self.run, self.docker, self.compose, self.admin = run, docker, compose, admin
        self.source_url, self.database, self.directory, self.output, self.env = source_url, database, Path(directory), Path(output), env

    def stop(self):
        self.run([*self.compose, "stop", "gateway", "projector", "imports", "app", "superset"])

    def start(self):
        self.run([*self.compose, "up", "-d", "--no-deps", "--wait", "app", "superset"])
        self.run([*self.compose, "up", "-d", "--no-deps", "--wait", "projector", "imports", "gateway"])

    def source_command(self, command):
        return [*self.docker, "run", "--rm", "-i", "--add-host", "host.docker.internal:host-gateway", "-e", "PGPASSWORD", POSTGRES,
            command, "-h", "host.docker.internal", "-p", str(self.source_url.port or 5432), "-U", self.source_url.username or "postgres"]

    def backup(self):
        self.stop()
        with (self.directory / "source.dump").open("wb") as file:
            self.run([*self.source_command("pg_dump"), "-Fc", "--no-owner", self.database], stdout=file,
                env={**self.env, "PGPASSWORD": self.source_url.password})
        for name in ("analytics", "superset_meta"):
            with (self.directory / f"{name}.dump").open("wb") as file:
                self.run([*self.compose, "exec", "-T", "analytics-postgres", "pg_dump", "-U", "postgres", "-Fc", "--no-owner", name], stdout=file)
        code = "import sys,tarfile\nwith tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:\n archive.add('/var/lib/city-runner/originals',arcname='originals')"
        with (self.directory / "originals.tar.gz").open("wb") as file:
            self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "-c", code], stdout=file)
        self.start()

    def restore_and_verify(self):
        # The latest deletion ledger is deliberately not restored from backup.
        self.stop()
        with self.admin.connect() as db:
            db.exec_driver_sql("DROP DATABASE " + self.database + " WITH (FORCE)")
            db.exec_driver_sql("CREATE DATABASE " + self.database)
        with (self.directory / "source.dump").open("rb") as file:
            self.run([*self.source_command("pg_restore"), "--exit-on-error", "--no-owner", "-d", self.database], stdin=file,
                env={**self.env, "PGPASSWORD": self.source_url.password})
        for name in ("analytics", "superset_meta"):
            owner = "superset_meta" if name == "superset_meta" else "postgres"
            self.run([*self.compose, "exec", "-T", "analytics-postgres", "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                "-c", f"DROP DATABASE {name} WITH (FORCE)", "-c", f"CREATE DATABASE {name} OWNER {owner}"])
        self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "scripts/superset/recovery_fixture.py", "restore-roles"])
        for name in ("analytics", "superset_meta"):
            with (self.directory / f"{name}.dump").open("rb") as file:
                role = ["--role", "superset_meta"] if name == "superset_meta" else []
                self.run([*self.compose, "exec", "-T", "analytics-postgres", "pg_restore", "-U", "postgres", *role, "--clean", "--if-exists", "--exit-on-error", "--no-owner", "-d", name], stdin=file)
        code = "import sys,tarfile\nwith tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:\n archive.extractall('/var/lib/city-runner',filter='data')"
        with (self.directory / "originals.tar.gz").open("rb") as file:
            self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "-c", code], stdin=file)
        self.run([*self.compose, "up", "-d", "--no-deps", "--wait", "superset"])
        audit = self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "scripts/superset/recovery_fixture.py", "audit-restored"], capture_output=True, text=True)
        restored = json.loads(audit.stdout)
        self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "-m", "backend.app.account_data", "--restore"])
        audit = self.run([*self.compose, "run", "--rm", "--no-deps", "-T", "app", "python", "scripts/superset/recovery_fixture.py", "audit-deleted"], capture_output=True, text=True)
        checks = {**restored, **json.loads(audit.stdout)}
        self.start()
        (self.output / "backup-restore.json").write_text(json.dumps({"checks": checks, "probe_passed": all(checks.values())}, indent=2) + "\n")
