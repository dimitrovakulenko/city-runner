"""Synthetic history/HTTP benchmark; creates and destroys its own UUID PostGIS DB."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid
from xml.etree.ElementTree import Element, SubElement, tostring

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from backend.app.coverage import rebuild_account_coverage
from backend.app.geography import import_osm_xml
from backend.app.gpx import parse_gpx
from backend.app.storage import LocalObjectStore


@dataclass
class HttpResult:
    status: int
    body: bytes
    elapsed_ms: float
    encoding: str
    wire_bytes: int

    def __iter__(self):
        return iter((self.status, self.body, self.elapsed_ms))

    def __getitem__(self, index):
        return (self.status, self.body, self.elapsed_ms)[index]


def worker(events_path):
    from backend.app.coverage import process_source_dataset
    from backend.app.corrections import process_private_object_cleanup
    from backend.app.uploads import process_upload
    from backend.app.worker import run_once
    engine = create_engine(os.environ['DATABASE_URL'])
    def timed(handler):
        def run(job):
            started = datetime.now(timezone.utc); begin = time.perf_counter()
            handler(job)
            event = {'job_id': str(job['id']), 'source_id': str(job['payload'].get('source_id')), 'kind': job['kind'],
                'queue_ms': (started - job['created_at']).total_seconds() * 1000,
                'handler_ms': (time.perf_counter() - begin) * 1000}
            with open(events_path, 'a') as log: log.write(json.dumps(event) + '\n')
        return run
    handlers = {'process_upload': timed(lambda job: process_upload(engine, job)),
        'match_coverage': timed(lambda job: process_source_dataset(engine, job)),
        'delete_private_object': lambda job: process_private_object_cleanup(job, engine=engine)}
    while True:
        if not run_once(engine, handlers): time.sleep(.1)


def geography(path: Path):
    root = Element('osm', version='0.6', generator='synthetic-history-benchmark')
    rows = []
    for city in range(2):
        west, south = 3.70 + city * .06, 51.0
        corners = [(west, south), (west + .04, south), (west + .04, south + .205), (west, south + .205)]
        for index, (lon, lat) in enumerate(corners):
            SubElement(root, 'node', id=str(1 + city * 10 + index), lon=str(lon), lat=str(lat))
        for index in range(4):
            way = SubElement(root, 'way', id=str(10 + city * 10 + index))
            for corner in [index, (index + 1) % 4]: SubElement(way, 'nd', ref=str(1 + city * 10 + corner))
        relation = SubElement(root, 'relation', id=str(900 + city))
        for index in range(4): SubElement(relation, 'member', type='way', ref=str(10 + city * 10 + index), role='outer')
        for key, value in {'type': 'boundary', 'boundary': 'administrative', 'admin_level': '8', 'name': f'Benchmark City {city + 1}'}.items():
            SubElement(relation, 'tag', k=key, v=value)
        for row in range(100):
            points = []
            for offset in range(10):
                node = 1000 + city * 10000 + row * 10 + offset
                lon, lat = west + .01 + offset * .0004, south + .002 + row * .002
                SubElement(root, 'node', id=str(node), lon=str(lon), lat=str(lat))
                points.append((node, lon, lat))
            way = SubElement(root, 'way', id=str(1000 + city * 100 + row))
            for node, _, _ in points: SubElement(way, 'nd', ref=str(node))
            SubElement(way, 'tag', k='highway', v='residential')
            SubElement(way, 'tag', k='name', v=f'Benchmark street {city + 1}-{row:03}')
            rows.append(points)
    path.write_bytes(tostring(root))
    return rows


def activity(index: int, samples: int, points, name: str):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=index % 730)
    coordinates = [points[point % len(points)] for point in range(samples)]
    track = ''.join(f'<trkpt lon="{lon}" lat="{lat}"><time>{(start + timedelta(seconds=point)).isoformat().replace("+00:00", "Z")}</time></trkpt>' for point, (_, lon, lat) in enumerate(coordinates))
    kind = 'walking' if index % 3 == 0 else 'running'
    content = f'<gpx version="1.1" creator="synthetic"><trk><name>{name}</name><type>{kind}</type><trkseg>{track}</trkseg></trk></gpx>'.encode()
    return content, parse_gpx(content)


def seed(engine, store, dataset: int, rows, accounts, main_count: int, secondary_count: int, samples: int):
    original_bytes = 0
    for owner_index, (account, _token) in enumerate(accounts):
        count = main_count if owner_index == 0 else secondary_count
        for base in range(0, count, 100):
            with engine.begin() as db:
                for index in range(base, min(base + 100, count)):
                    points = rows[(index + owner_index * 7) % len(rows)]
                    content, parsed = activity(index, samples, points, f'Benchmark {owner_index}-{index}')
                    key = store.write(content)
                    original_bytes += len(content)
                    activity_id = db.execute(text('''INSERT INTO activities(user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                        VALUES (:owner,:name,:date,:kind,true,0,CAST(:tracks AS json),CAST(:timestamps AS json)) RETURNING id'''), {
                        'owner': account, 'name': parsed.name, 'date': parsed.date, 'kind': parsed.activity_type,
                        'tracks': json.dumps(parsed.tracks, separators=(',', ':')), 'timestamps': json.dumps(parsed.timestamps, separators=(',', ':')),
                    }).scalar_one()
                    source_id = db.execute(text('''INSERT INTO activity_sources(account_id,activity_id,source_kind,content_hash,private_object_key,status)
                        VALUES (:owner,:activity,'gpx',:hash,:key,'succeeded') RETURNING id'''), {
                        'owner': account, 'activity': activity_id, 'hash': hashlib.sha256(content).hexdigest(), 'key': key,
                    }).scalar_one()
                    job = db.execute(text('''INSERT INTO jobs(account_id,kind,dedupe_key,status,attempts)
                        VALUES (:owner,'match_coverage',:key,'succeeded',1) RETURNING id'''), {
                        'owner': account, 'key': f'benchmark:{source_id}',
                    }).scalar_one()
                    db.execute(text('''INSERT INTO coverage_source_runs(account_id,source_id,source_revision,dataset_id,job_id,status,
                        sample_count,supported_sample_count,unsupported_sample_count,matched_node_count)
                        VALUES (:owner,:source,1,:dataset,:job,'succeeded',:samples,:samples,0,:nodes)'''), {
                        'owner': account, 'source': source_id, 'dataset': dataset, 'job': job, 'samples': samples, 'nodes': len(points),
                    })
                    db.execute(text('''INSERT INTO source_node_contributions(account_id,source_id,source_revision,dataset_id,node_id)
                        SELECT :owner,:source,1,:dataset,node FROM unnest(CAST(:nodes AS bigint[])) node'''), {
                        'owner': account, 'source': source_id, 'dataset': dataset, 'nodes': [point[0] for point in points],
                    })
            if owner_index == 0 and base % 1000 == 0:
                print(f'Seeded heavy account: {min(base + 100, count)}/{count}', flush=True)
        rebuild_account_coverage(engine, account, dataset)
    with engine.begin() as db: db.execute(text('ANALYZE'))
    return original_bytes


def statistics(values):
    ordered = sorted(values)
    return {'samples': len(values), 'p50_ms': round(ordered[math.ceil(len(values) * .5) - 1], 2),
            'p95_ms': round(ordered[math.ceil(len(values) * .95) - 1], 2), 'max_ms': round(max(values), 2)}


def request(base: str, path: str, token: str | None = None, data=None, content_type=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    headers['Accept-Encoding'] = 'gzip'
    if content_type: headers['Content-Type'] = content_type
    started = time.perf_counter()
    try:
        with urlopen(Request(base + path, data=data, headers=headers), timeout=150) as response:
            body = response.read(); status = response.status; encoding = response.headers.get('Content-Encoding', 'identity')
    except HTTPError as error:
        body = error.read(); status = error.code; encoding = error.headers.get('Content-Encoding', 'identity')
    except (URLError, TimeoutError) as error:
        body = str(error).encode(); status = 0; encoding = 'identity'
    wire_bytes = len(body)
    if encoding == 'gzip': body = gzip.decompress(body)
    return HttpResult(status, body, (time.perf_counter() - started) * 1000, encoding, wire_bytes)


def summarize(results, cases):
    report = {}
    for name, _path in cases:
        selected = [result for result in results if result[0] == name]
        if not selected: continue
        report[name] = {**statistics([result[2] for result in selected]),
            'successful': sum(result[1] == 200 for result in selected),
            'status_counts': {str(status): sum(result[1] == status for result in selected) for status in sorted({result[1] for result in selected})},
            'max_decoded_response_bytes': max(result[3] for result in selected), 'max_theoretical_gzip_bytes': max(result[4] for result in selected),
            'truncated_responses': sum(result[5] for result in selected), 'max_transferred_body_bytes': max(result[6] for result in selected),
            'content_encodings': sorted({result[7] for result in selected})}
    return report


def sample_request(base, account, case):
    name, path = case
    result = request(base, path, account[1]); status, body, elapsed = result
    value = json.loads(body) if status == 200 else None
    if status == 200 and name == 'activities':
        owner = account[0].removeprefix('benchmark-')
        assert all(item['name'].startswith((f'Benchmark {owner}-', 'Measured import ' if owner == '0' else f'Benchmark {owner}-')) for item in value['items']), 'owner isolation failure'
    if status == 200 and name.startswith('map'):
        assert value['limits']['geometry_bytes']['returned'] <= value['limits']['geometry_bytes']['limit']
    return name, status, elapsed, len(body), len(gzip.compress(body)), bool(value and name.startswith('map') and any(limit['truncated'] for limit in value['limits'].values())), result.wire_bytes, result.encoding


def sample_phase(base, cases, accounts, repeats, heavy=False):
    results = []
    def run(owner_index, case, repeat):
        owner = 0 if heavy else owner_index
        return sample_request(base, accounts[owner], case)
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        # Each case runs with a fresh 20-client burst; no single-client warm result is presented as load acceptance.
        for case in cases:
            for repeat in range(repeats):
                results.extend(pool.map(lambda owner: run(owner, case, repeat), range(len(accounts))))
    return summarize(results, cases)


def cpu_seconds(value):
    days, _, clock = value.rpartition('-')
    parts = [float(part) for part in clock.split(':')]
    return (int(days) * 86400 if days else 0) + sum(part * 60 ** index for index, part in enumerate(reversed(parts)))


class ProcessMetrics:
    def __init__(self, engine, children):
        self.engine, self.children = engine, children
        self.baseline, self.latest = {}, {}
        self.peak = {'api': 0, 'worker': 0, 'postgresql': 0}
        self.sample()

    def sample(self):
        with self.engine.connect() as db:
            postgres = db.execute(text('SELECT pid FROM pg_stat_activity WHERE datname=current_database()')).scalars().all()
        groups = {'api': [self.children[0].pid], 'worker': [self.children[1].pid], 'postgresql': postgres}
        ids = sorted({pid for pids in groups.values() for pid in pids})
        result = subprocess.run(['ps', '-o', 'pid=,rss=,time=', '-p', ','.join(map(str, ids))], capture_output=True, text=True, check=True)
        memory = {}
        for line in result.stdout.splitlines():
            pid, rss, elapsed = line.split(); pid = int(pid)
            memory[pid] = int(rss) * 1024
            self.latest[pid] = cpu_seconds(elapsed)
            self.baseline.setdefault(pid, self.latest[pid] if pid in postgres else 0)
        for name, pids in groups.items():
            self.peak[name] = max(self.peak[name], sum(memory.get(pid, 0) for pid in pids))
        self.postgres_ids = getattr(self, 'postgres_ids', set()) | set(postgres)

    def report(self):
        groups = {'api': [self.children[0].pid], 'worker': [self.children[1].pid], 'postgresql': self.postgres_ids}
        return {'sampled_peak_rss_bytes': self.peak, 'sampled_cpu_seconds': {
            name: round(sum(self.latest.get(pid, 0) - self.baseline.get(pid, 0) for pid in pids), 3) for name, pids in groups.items()},
            'sampling_seconds': 1, 'limitations': 'PostgreSQL includes backends connected to this disposable database; summed RSS counts shared pages repeatedly. Short-lived processes between samples and cluster background workers are excluded. Local loopback, no VM/native/provider acceptance.'}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker-events': return worker(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activities', type=int, default=5000)
    parser.add_argument('--samples', type=int, default=1000)
    parser.add_argument('--users', type=int, default=20)
    parser.add_argument('--secondary-activities', type=int, default=100)
    parser.add_argument('--import-files', type=int, default=20)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (1 <= args.activities <= 10000 and 10 <= args.samples <= 100000 and 1 <= args.users <= 20 and
            1 <= args.secondary_activities <= 100 and 1 <= args.import_files <= 100 and 1 <= args.repeats <= 10):
        parser.error('workload is outside safe benchmark bounds')
    if (args.activities + (args.users - 1) * args.secondary_activities + args.import_files) * args.samples > 7_500_000:
        parser.error('benchmark permits at most 7.5 million synthetic samples')
    admin_url = make_url(os.environ['POSTGIS_ADMIN_DATABASE_URL']).set(database='postgres')
    assert admin_url.host in (None, 'localhost', '127.0.0.1', '::1'), 'benchmark requires local PostGIS'
    database = 'city_runner_test_' + uuid.uuid4().hex[:12]
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT')
    engine = None; created = False; children = []; stopped = threading.Event(); metrics = None; monitoring = None
    started = time.perf_counter(); cpu_start = time.process_time()
    report = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'Local synthetic GPX file and HTTP benchmark; no VM/native/provider acceptance',
        'fixture_method': 'Production GPX parser and original storage; bulk seeded qualified source/job/run/node support. Fixture loading is not import timing. Each activity repeats ten distinct original nodes; Measured files are uploaded sequentially alongside concurrent browsing.',
        'environment': {'os': platform.platform(), 'architecture': platform.machine(), 'python': platform.python_version(),
            'logical_cpus': os.cpu_count(), 'physical_memory_bytes': os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')},
        'workload': vars(args) | {'output': str(args.output)}}
    try:
        with admin.connect() as db: db.exec_driver_sql(f'CREATE DATABASE "{database}"')
        created = True
        with tempfile.TemporaryDirectory(prefix='city-runner-benchmark-') as temporary:
            directory = Path(temporary)
            url = admin_url.set(database=database).render_as_string(hide_password=False)
            os.environ['DATABASE_URL'] = url; os.environ['UPLOAD_STORAGE_DIR'] = str(directory / 'sources')
            os.environ['ACCOUNT_DELETION_LEDGER'] = str(directory / 'privacy' / 'deletions.jsonl')
            from backend.app.account_deletion_journal import initialize_ledger
            initialize_ledger()
            config = Config(str(ROOT / 'backend/alembic.ini')); config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
            command.upgrade(config, 'head'); engine = create_engine(url, pool_size=5, max_overflow=5)
            path = directory / 'geography.osm'; rows = geography(path)
            dataset = import_osm_xml(engine, path, region='Benchmark synthetic region', city_relation_ids=[900, 901],
                source_timestamp='2026-10-07T00:00:00Z', coverage_mode='complete', coverage_evidence='Synthetic history benchmark')['dataset_id']
            accounts = [(f'benchmark-{index}', uuid.uuid4().hex * 2) for index in range(args.users)]
            with engine.begin() as db:
                for account, token in accounts:
                    db.execute(text('INSERT INTO accounts(id) VALUES (:id)'), {'id': account})
                    db.execute(text('INSERT INTO sessions(token_digest,account_id,expires_at) VALUES (:digest,:id,:expires)'), {
                        'digest': hashlib.sha256(token.encode()).hexdigest(), 'id': account, 'expires': datetime.now(timezone.utc) + timedelta(hours=2)})
                report['environment']['postgresql'] = db.execute(text('SELECT version()')).scalar_one()
                report['environment']['postgis'] = db.execute(text('SELECT postgis_lib_version()')).scalar_one()
            store = LocalObjectStore(); fixture_start = time.perf_counter()
            report['storage'] = {'seed_original_bytes': seed(engine, store, int(dataset), rows, accounts, args.activities, args.secondary_activities, args.samples)}
            report['fixture_seconds'] = round(time.perf_counter() - fixture_start, 3)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = os.environ.copy(); env['PYTHONDONTWRITEBYTECODE'] = '1'
            log = (directory / 'process.log').open('w')
            try:
                children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.app.main:app', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'warning'], cwd=ROOT, env=env, stdout=log, stderr=log))
                events_path = directory / 'worker-events.jsonl'
                children.append(subprocess.Popen([sys.executable, str(Path(__file__)), '--worker-events', str(events_path)], cwd=ROOT, env=env, stdout=log, stderr=log))
                metrics = ProcessMetrics(engine, children)
                def monitor():
                    while not stopped.wait(1):
                        metrics.sample()
                monitoring = threading.Thread(target=monitor, daemon=True); monitoring.start()
                for _ in range(200):
                    try:
                        if request(base, '/docs')[0] == 200: break
                    except OSError: pass
                    if any(child.poll() is not None for child in children): raise RuntimeError('benchmark API/worker failed to start')
                    time.sleep(.1)
                else: raise RuntimeError('benchmark API start timeout')
                close = 'bbox=3.708,51.000,3.716,51.006&zoom=17'
                selection = 'date_from=2024-01-01&date_to=2024-01-31&source=gpx&activity_type=running&coverage_scope=filtered'
                cases = [('map-overview', '/api/map?bbox=3.7,51,3.8,51.205&zoom=13'), ('map-close', '/api/map?' + close),
                    ('map-filtered', '/api/map?' + close + '&' + selection), ('progress', '/api/progress'),
                    ('progress-filtered', '/api/progress?' + selection), ('activities', '/api/activities?page=1&page_size=20'),
                    ('sorted-streets', f'/api/cities/{900}/streets?dataset_id={dataset}&sort=completion-desc')]
                # Obtain the actual internal city ID; public relation IDs are not allocated city IDs.
                cities = json.loads(request(base, f'/api/cities?dataset_id={dataset}', accounts[0][1])[1])
                cases[-1] = ('sorted-streets', f'/api/cities/{cities["items"][0]["id"]}/streets?dataset_id={dataset}&sort=completion-desc')
                report['warmup_statuses'] = {name: request(base, path, accounts[0][1]).status for name, path in cases}
                print('Measuring distinct-user and heavy-account concurrent HTTP bursts', flush=True)
                report['distinct_users'] = sample_phase(base, cases, accounts, args.repeats)
                report['heavy_account'] = sample_phase(base, cases, accounts, args.repeats, heavy=True)
                imports = []; ingest = []; source_ids = []; import_jobs = []; browsing = []; importing = threading.Event()
                def browse(owner):
                    index = 0
                    while not importing.is_set():
                        browsing.append(sample_request(base, accounts[owner], cases[index % len(cases)]))
                        index += 1; importing.wait(.1)
                browser_pool = ThreadPoolExecutor(max_workers=len(accounts))
                browser_futures = [browser_pool.submit(browse, owner) for owner in range(len(accounts))]
                try:
                    measure_imports(base, accounts[0], args, rows, imports, ingest, source_ids, import_jobs)
                finally:
                    importing.set(); browser_pool.shutdown(wait=True)
                    for future in browser_futures: future.result()
                report['browse_during_imports'] = summarize(browsing, cases)
                events = [json.loads(line) for line in events_path.read_text().splitlines()]
                ingestion = [event for event in events if event['job_id'] in import_jobs]
                matching = [event for event in events if event['kind'] == 'match_coverage' and event['source_id'] in source_ids]
                assert len(ingestion) == args.import_files and len(matching) == args.import_files
                report['real_http_import'] = {'upload_acceptance': statistics(imports), 'ingestion_queue': statistics([event['queue_ms'] for event in ingestion]),
                    'ingestion_handler': statistics([event['handler_ms'] for event in ingestion]), 'matching_queue': statistics([event['queue_ms'] for event in matching]),
                    'matching_handler': statistics([event['handler_ms'] for event in matching]), 'upload_to_ready_including_poll_interval': statistics(ingest), 'poll_interval_ms': 100, 'browsing_think_time_ms': 100}
                with engine.connect() as db:
                    report['storage']['database_bytes'] = int(db.execute(text('SELECT pg_database_size(current_database())')).scalar_one())
                    report['verified'] = dict(db.execute(text('''SELECT count(*) AS activities,sum(json_array_length(tracks->0)) AS gps_samples FROM activities WHERE user_id=:owner'''), {'owner': accounts[0][0]}).mappings().one())
                    assert report['verified'] == {'activities': args.activities + args.import_files, 'gps_samples': (args.activities + args.import_files) * args.samples}
                    assert db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:owner AND status='succeeded'"), {'owner': accounts[0][0]}).scalar_one() == args.activities + args.import_files
                    assert db.execute(text("SELECT count(*) FROM coverage_source_runs WHERE account_id=:owner AND status='succeeded'"), {'owner': accounts[0][0]}).scalar_one() == args.activities + args.import_files
                    report['verified']['cities'] = len(cities['items'])
                    report['verified']['distinct_users'] = len(accounts)
                    report['verified']['heavy_source_contribution_rows'] = db.execute(text('SELECT count(*) FROM source_node_contributions WHERE account_id=:owner'), {'owner': accounts[0][0]}).scalar_one()
                    report['verified']['all_source_contribution_rows'] = db.execute(text('SELECT count(*) FROM source_node_contributions')).scalar_one()
                    report['verified']['geography_nodes'] = db.execute(text('SELECT count(*) FROM street_nodes')).scalar_one()
                    report['verified']['geography_streets'] = db.execute(text('SELECT count(*) FROM streets')).scalar_one()
                report['storage']['final_original_bytes'] = sum(path.stat().st_size for path in store.root.rglob('*') if path.is_file() and path.suffix in ('.gpx', '.fit'))
                report['targets'] = {'warm_viewport_p95_under_1000ms': all(result['p95_ms'] < 1000 and result['successful'] == result['samples'] for phase in ('distinct_users', 'heavy_account', 'browse_during_imports') for name, result in report[phase].items() if name.startswith('map')),
                    'map_transferred_body_under_1MiB': all(result['max_transferred_body_bytes'] <= 1024 * 1024 for phase in ('distinct_users', 'heavy_account', 'browse_during_imports') for name, result in report[phase].items() if name.startswith('map'))}
            finally:
                stopped.set()
                if monitoring: monitoring.join(timeout=10)
                if metrics: metrics.sample()
                for child in children:
                    if child.poll() is None: child.terminate()
                for child in children:
                    try: child.wait(timeout=10)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                log.close()
            report['server_errors'] = sorted(set(line.strip()[:400] for line in (directory / 'process.log').read_text().splitlines() if 'Error:' in line or 'Exception:' in line))
            report['resources'] = metrics.report() | {'driver_cpu_seconds_including_fixture': round(time.process_time() - cpu_start, 3)}
    finally:
        if engine: engine.dispose()
        if created:
            with admin.connect() as db: db.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    report['disposable_cleanup'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'targets': report['targets'], 'verified': report['verified'], 'cleanup': True}), flush=True)


def measure_imports(base, account, args, rows, imports, ingest, source_ids, import_jobs):
    for index in range(args.import_files):
        content, _parsed = activity(index, args.samples, rows[index % len(rows)], f'Measured import {index}')
        boundary = 'synthetic-benchmark'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="benchmark.gpx"\r\nContent-Type: application/gpx+xml\r\n\r\n'.encode() + content + f'\r\n--{boundary}--\r\n'.encode())
        begin = time.perf_counter(); status, raw, upload_ms = request(base, '/api/uploads', account[1], body, 'multipart/form-data; boundary=' + boundary)
        assert status == 202, f'upload HTTP {status}'
        job = json.loads(raw); imports.append(upload_ms); source_ids.append(job['id']); import_jobs.append(job['job_id'])
        for _ in range(600):
            source = json.loads(request(base, '/api/uploads/' + job['id'], account[1])[1])
            assert source['status'] != 'failed', 'synthetic import failed'
            if source['status'] == 'succeeded':
                progress = json.loads(request(base, '/api/progress', account[1])[1])
                if progress['state'] == 'ready': break
            time.sleep(.1)
        else: raise RuntimeError('synthetic import processing timeout')
        ingest.append((time.perf_counter() - begin) * 1000)

if __name__ == '__main__': main()
