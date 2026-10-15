"""Measure fixed-owner protected views in a newly created disposable app DB.

Requires the separate city-runner-superset-view-probe PostGIS container. Never
opens an existing application DB. Reuses the retained-history benchmark fixtures.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.app.storage import LocalObjectStore
from backend.app.owner_guard import account_lock_key, owner_lock

spec = importlib.util.spec_from_file_location('history_benchmark', ROOT / 'scripts/dev/history-benchmark.py')
history = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = history
spec.loader.exec_module(history)


def run(args, report):
    # Explicit disposable endpoint; inherited DATABASE_URL is never consulted.
    admin_url = make_url(os.getenv('SUPERSET_PROBE_ADMIN_DATABASE_URL',
        'postgresql+psycopg://postgres:disposable-view-probe@127.0.0.1:55446/postgres'))
    suffix = uuid.uuid4().hex[:12]
    database = f'city_runner_analytics_probe_{suffix}'
    admin = create_engine(admin_url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    roles = [f'cr_view_probe_{label}_{suffix}' for label in ('a', 'b')]
    schemas = [f'analytics_{label}' for label in ('a', 'b')]
    accounts = [(f'view-probe-{label}-{suffix}', secrets.token_hex(32)) for label in ('a', 'b')]
    passwords = [secrets.token_hex(32) for _ in roles]
    readers = []
    created = False
    engine = None
    try:
        with admin.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{database}"')
        created = True
        url = admin_url.set(database=database)
        os.environ['DATABASE_URL'] = url.render_as_string(hide_password=False)
        config = Config(str(ROOT / 'backend/alembic.ini'))
        config.set_main_option('sqlalchemy.url', os.environ['DATABASE_URL'].replace('%', '%%'))
        command.upgrade(config, 'head')
        engine = create_engine(url)
        with tempfile.TemporaryDirectory(prefix='superset-view-probe-') as directory:
            root = Path(directory)
            rows = history.geography(root / 'synthetic.osm')
            dataset = history.import_osm_xml(engine, root / 'synthetic.osm', region=f'view-probe-{suffix}',
                city_relation_ids=[900, 901], source_timestamp='2026-10-15T00:00:00Z',
                coverage_mode='complete', coverage_evidence='Synthetic protected-view feasibility measurement')['dataset_id']
            with engine.begin() as db:
                for account, _ in accounts:
                    db.execute(text('INSERT INTO accounts(id) VALUES (:id)'), {'id': account})
            history.seed(engine, LocalObjectStore(root / 'originals'), dataset, rows, accounts,
                         args.activities, 20, args.samples)
        with engine.begin() as db:
            # A second retained format must never double an activity total.
            db.execute(text("""INSERT INTO activity_sources(account_id,activity_id,source_kind,content_hash,status)
                SELECT account_id,activity_id,'fit',repeat('f',64),'succeeded' FROM activity_sources
                WHERE account_id=:owner ORDER BY id LIMIT 1"""), {'owner': accounts[0][0]})
            for index, (role, schema, password) in enumerate(zip(roles, schemas, passwords)):
                # Identifiers/owner literals below are generated hexadecimal fixtures only.
                db.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 2")
                db.exec_driver_sql(f'CREATE SCHEMA {schema}')
                db.exec_driver_sql(f"""CREATE VIEW {schema}.activities WITH (security_barrier=true) AS
                    SELECT activity.id::text AS activity_id,activity.date,activity.activity_type,
                        activity.distance_m,activity.duration_s,activity.pace_s_per_km,activity.statistics_version
                    FROM public.activities activity WHERE activity.user_id='{accounts[index][0]}'
                    AND EXISTS (SELECT 1 FROM public.activity_sources source
                        WHERE source.account_id=activity.user_id AND source.activity_id=activity.id
                          AND source.status='succeeded' AND source.source_kind IN ('gpx','fit'))""")
                db.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{database}" TO {role}')
                db.exec_driver_sql(f'GRANT USAGE ON SCHEMA {schema} TO {role}')
                db.exec_driver_sql(f'GRANT SELECT ON {schema}.activities TO {role}')
        for index, (role, schema, password) in enumerate(zip(roles, schemas, passwords)):
            reader = create_engine(url.set(username=role, password=password), pool_size=2, max_overflow=0)
            readers.append(reader)
            with reader.connect() as read:
                read.execute(text("SELECT set_config('city_runner.account_id', :other, false)"), {'other': accounts[1-index][0]})
                count = read.exec_driver_sql(f'SELECT count(*) FROM {schema}.activities').scalar_one()
                report['checks'][f'immutable_owner_{index}'] = count == (args.activities if index == 0 else 20)
                report['checks'][f'raw_table_denied_{index}'] = not read.execute(text("SELECT has_table_privilege(current_user,'public.activities','SELECT')")).scalar_one()
                report['checks'][f'credentials_denied_{index}'] = not read.execute(text("SELECT has_table_privilege(current_user,'public.sessions','SELECT')")).scalar_one()
                report['checks'][f'foreign_schema_denied_{index}'] = not read.execute(text('SELECT has_schema_privilege(current_user,:schema,\'USAGE\')'), {'schema': schemas[1-index]}).scalar_one()
                report['checks'][f'public_schema_not_writable_{index}'] = not read.execute(text("SELECT has_schema_privilege(current_user,'public','CREATE')")).scalar_one()
                dangerous = read.execute(text("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosecdef
                      AND has_schema_privilege(current_user,n.oid,'USAGE')
                      AND has_function_privilege(current_user,p.oid,'EXECUTE')""")).scalar_one()
                report['checks'][f'privileged_application_functions_denied_{index}'] = dangerous == 0
        def query(index):
            start = time.perf_counter()
            with readers[index].connect() as db:
                db.exec_driver_sql("SET statement_timeout='1500ms'")
                result = db.exec_driver_sql(f'''SELECT date,activity_type,count(*),sum(distance_m),
                    count(*) FILTER (WHERE distance_m IS NULL) FROM {schemas[index]}.activities
                    GROUP BY date,activity_type ORDER BY date,activity_type''').all()
                assert sum(row[2] for row in result) == (args.activities if index == 0 else 20)
            return (time.perf_counter() - start) * 1000
        with ThreadPoolExecutor(4) as pool:
            samples = list(pool.map(query, [0, 1] * 10))
        report['observations'] = {'activities': args.activities, 'gps_samples': args.activities * args.samples,
            'concurrency': 4, 'grouped_activity_query': history.statistics(samples),
            'scope': 'activity-view feasibility only; coverage/history view semantics and mixed ingestion load remain D44/D47'}
        report['checks']['grouped_queries_within_budget'] = max(samples) < 1500
        # SELECT-only roles can still take application advisory locks in a shared
        # database. Prove the collision before choosing the production boundary.
        collision = False
        with readers[0].connect() as read:
            read.execute(text('SELECT pg_advisory_lock(hashtextextended(:key,0))'), {'key': account_lock_key(accounts[0][0])})
            try:
                with engine.begin() as db:
                    db.exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                    owner_lock(db, accounts[0][0])
            except DBAPIError as error:
                collision = getattr(error.orig, 'sqlstate', None) == '55P03'
            finally:
                read.exec_driver_sql('SELECT pg_advisory_unlock_all()')
        report['checks']['live_view_advisory_lock_collision_detected'] = collision
        report['observations']['decision'] = 'Separate projected analytics database: live SQL authors can block application owner locks despite SELECT-only grants.'
        with engine.connect() as db:
            report['observations']['database_version'] = db.exec_driver_sql('SELECT version()').scalar_one()
            report['observations']['postgis_version'] = db.exec_driver_sql('SELECT postgis_full_version()').scalar_one()
    finally:
        for reader in readers:
            reader.dispose()
        if engine:
            engine.dispose()
        with admin.connect() as db:
            if created:
                db.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            for role in roles:
                db.exec_driver_sql(f'DROP ROLE IF EXISTS {role}')
        admin.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--activities', type=int, default=5000, choices=range(1, 10001))
    parser.add_argument('--samples', type=int, default=1000, choices=range(1, 1001))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'checks': {}}
    try:
        run(args, report)
        report['checks']['completed_and_cleaned'] = True
    except Exception as error:
        report['checks']['completed_and_cleaned'] = False
        report['error'] = f'{type(error).__name__}: {error}'
    report['probe_passed'] = all(report['checks'].values())
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['probe_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
