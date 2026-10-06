"""Disposable local browser QA backend; never opens an existing activity database."""
import hashlib
import os
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
import uvicorn
from backend.app.geography import import_osm_xml
from backend.app.main import create_app
from backend.app.uploads import process_upload
from backend.app.coverage import process_source_dataset
from backend.app.corrections import process_private_object_cleanup
from backend.app.worker import run_once
from backend.tests.test_fit import fit_fixture, semicircles

ADMIN_URL = os.environ['POSTGIS_ADMIN_DATABASE_URL']
database = 'city_runner_test_' + uuid.uuid4().hex[:12]
url = make_url(ADMIN_URL).set(database=database).render_as_string(hide_password=False)
admin = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
engine = None
created = False
stop = threading.Event()
worker = None
fixtures = Path(__file__).parent / '.fixtures'
fixtures.mkdir(exist_ok=True)

def geography():
    root = Element('osm', version='0.6', generator='city-runner-synthetic-web-test')
    boundaries = {1: (3.71, 51.04), 2: (3.74, 51.04), 3: (3.74, 51.065), 4: (3.71, 51.065)}
    for identifier, (lon, lat) in boundaries.items():
        SubElement(root, 'node', id=str(identifier), lon=str(lon), lat=str(lat))
    for index, name in enumerate(['Maple Lane', 'Canal Road', 'Garden Way', 'Orchard Street', 'Birch Avenue', 'River Walk', 'Park Lane', 'Willow Road']):
        way = SubElement(root, 'way', id=str(20 + index))
        for offset in range(5):
            identifier = 1000 + index * 10 + offset
            SubElement(root, 'node', id=str(identifier), lon=str(3.716 + offset * .004), lat=str(51.044 + index * .002))
            SubElement(way, 'nd', ref=str(identifier))
        SubElement(way, 'tag', k='highway', v='residential')
        SubElement(way, 'tag', k='name', v=name)
    for identifier, refs in [(10, [1, 2]), (11, [2, 3]), (12, [3, 4]), (13, [4, 1])]:
        way = SubElement(root, 'way', id=str(identifier))
        for ref in refs: SubElement(way, 'nd', ref=str(ref))
    relation = SubElement(root, 'relation', id='900')
    for identifier in [10, 11, 12, 13]: SubElement(relation, 'member', type='way', ref=str(identifier), role='outer')
    for key, value in {'type': 'boundary', 'boundary': 'administrative', 'admin_level': '8', 'name': 'Demo City'}.items(): SubElement(relation, 'tag', k=key, v=value)
    return tostring(root)

def gpx(name, rows):
    segments = ''.join('<trkseg>' + ''.join(f'<trkpt lat="{51.044 + row * .002}" lon="{3.716 + offset * .004}"><time>2026-10-06T06:{row:02}:{offset:02}Z</time></trkpt>' for offset in range(5)) + '</trkseg>' for row in rows)
    return f'<gpx version="1.1"><trk><name>{name}</name><type>running</type>{segments}</trk></gpx>'.encode()

try:
    with admin.connect() as db: db.exec_driver_sql(f'CREATE DATABASE "{database}"')
    created = True
    os.environ['DATABASE_URL'] = url
    with tempfile.TemporaryDirectory(prefix='city-runner-web-test-') as temporary:
        os.environ['UPLOAD_STORAGE_DIR'] = temporary + '/sources'
        config = Config(str(ROOT / 'backend/alembic.ini'))
        config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
        command.upgrade(config, 'head')
        engine = create_engine(url, pool_pre_ping=True)
        with engine.begin() as db:
            for account in ['alice', 'bob']:
                db.execute(text('INSERT INTO accounts(id) VALUES (:id)'), {'id': 'web-test-' + account})
                db.execute(text('INSERT INTO sessions(token_digest,account_id,expires_at) VALUES (:digest,:id,:expires)'), {
                    'digest': hashlib.sha256(('web-synthetic-' + account).encode()).hexdigest(), 'id': 'web-test-' + account,
                    'expires': datetime.now(timezone.utc) + timedelta(hours=4)})
        osm = Path(temporary) / 'synthetic.osm'; osm.write_bytes(geography())
        if os.environ.get('WEB_PREVIEW_GENT_OSM'):
            from gent_fixtures import prepare_gent
            prepare_gent(engine, os.environ['WEB_PREVIEW_GENT_OSM'], fixtures)
        elif os.environ.get('WEB_PREVIEW_EMPTY') != '1':
            import_osm_xml(engine, osm, region='Demo Region', city_relation_ids=[900], source_timestamp='2026-10-06T00:00:00Z',
                coverage_mode='complete', coverage_evidence='Synthetic browser QA only')
        app = create_app(engine)
        handlers = {'process_upload': lambda job: process_upload(engine, job), 'match_coverage': lambda job: process_source_dataset(engine, job),
            'delete_private_object': lambda job: process_private_object_cleanup(job)}
        with TestClient(app) as client:
            seeds = [] if os.environ.get('WEB_PREVIEW_EMPTY') == '1' else [('Morning loop [synthetic]', [0, 1]), ('A little further [synthetic]', [3, 4]), ('Evening stroll [synthetic]', [6])]
            for name, rows in seeds:
                response = client.post('/api/uploads', headers={'Authorization': 'Bearer web-synthetic-alice'}, files={'file': ('seed.gpx', gpx(name, rows), 'application/gpx+xml')})
                response.raise_for_status()
            while run_once(engine, handlers): pass
        (fixtures / 'browser.gpx').write_bytes(gpx('Browser GPX [synthetic]', [2]))
        (fixtures / 'browser.fit').write_bytes(fit_fixture(records=[(1167609600 + offset, semicircles(51.044 + 5 * .002), semicircles(3.716 + offset * .004)) for offset in range(5)]))
        valid_fit = (fixtures / 'browser.fit').read_bytes()
        (fixtures / 'corrupt.fit').write_bytes(valid_fit[:-1] + bytes([valid_fit[-1] ^ 1]))
        def work():
            while not stop.is_set():
                if not run_once(engine, handlers): stop.wait(.15)
        worker = threading.Thread(target=work, daemon=True); worker.start()
        try:
            uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('WEB_TEST_API_PORT', '8003')), log_level='warning')
        finally:
            stop.set()
            worker.join(timeout=5)
finally:
    stop.set()
    if worker: worker.join(timeout=5)
    if engine: engine.dispose()
    if created:
        with admin.connect() as db:
            db.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()'), {'name': database})
            db.exec_driver_sql(f'DROP DATABASE "{database}"')
    admin.dispose()
