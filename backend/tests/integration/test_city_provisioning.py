import json
import hashlib
import os
import tempfile
import unittest
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.city_provisioning import (
    _enroll_existing_sources,
    _enqueue_country_page,
    _country_task_key,
    _perform_public_request,
    _provider_versions,
    _world_candidate_countries,
    enqueue_preparation_request,
    process_city_provisioning_step,
    retry_coverage_preparation,
)
from backend.app.coverage import (_dataset_applicability, get_account_coverage, queue_source_coverage,
                                  _refresh_activity_processing, track_may_touch_dataset_sql)
from backend.app.main import create_app
from backend.app.map_imports import process_map_task_step
from backend.tests.integration.test_map_api import _city_fixture

DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = __import__("re").compile(r"city_runner_test_[0-9a-f]{12}\Z")


class _SyntheticProvider:
    def fetch_world_catalog(self):
        return {"regions": [{"country_code": "BE", "geometry": {
            "type": "Polygon", "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
        }}]}

    def fetch_country_catalog(self, country, levels):
        assert country == "BE" and levels == [8]
        return {"source_timestamp": "2026-10-13T00:00:00+00:00", "cities": [
            {"relation_id": 900, "name": "Synthetic City", "bounds": [-0.01, -0.01, 0.01, 0.01]},
            {"relation_id": 901, "name": "Gap Only Candidate", "bounds": [0.0025, 0.0005, 0.0035, 0.0015]},
        ]}

    def fetch_city_extract(self, relation_id):
        assert relation_id == 900
        return {"source_timestamp": "2026-10-13T00:00:00+00:00", "xml": _city_fixture()}


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class CityProvisioningPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("city provisioning tests require a generated disposable PostGIS database")
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.account = f"city-provision-{uuid.uuid4().hex}"
        self.unrelated_region = f"city-provision-unrelated-{uuid.uuid4().hex}"
        self.activity_id = 7_100_000_000_000_000 + int(uuid.uuid4().hex[:12], 16)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_provider = os.environ.get("CITY_GEOGRAPHY_PROVIDER")
        self.previous_storage = os.environ.get("MAP_IMPORT_STORAGE_ROOT")
        self.addCleanup(self._restore_environment)
        os.environ["CITY_GEOGRAPHY_PROVIDER"] = "overpass"
        os.environ["MAP_IMPORT_STORAGE_ROOT"] = str(Path(self.tmp.name) / "objects")
        self.provider_patch = patch("backend.app.city_geography_provider.get_city_geography_provider",
                                    return_value=_SyntheticProvider())
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        Path(os.environ["MAP_IMPORT_STORAGE_ROOT"]).mkdir(mode=0o700)
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": self.account})
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'Synthetic city sample','2026-10-13','run',FALSE,0,
                    CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": self.activity_id, "account": self.account,
                "tracks": json.dumps([[[0.002, 0.001], [0.004, 0.001]]]),
                "timestamps": json.dumps([[None, None]]),
            })
            self.source_id = int(db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'gpx','succeeded') RETURNING id"""), {
                "account": self.account, "activity": self.activity_id,
            }).scalar_one())
            enqueue_preparation_request(db, account_id=self.account, source_id=self.source_id, source_revision=1)

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM accounts WHERE id=:id"), {"id": self.account})
            db.execute(text("DELETE FROM city_public_tasks WHERE task_key LIKE 'city:900:%'"))
            db.execute(text("DELETE FROM city_public_tasks WHERE task_key LIKE 'world:stale:%'"))
            db.execute(text("""UPDATE city_provider_admission SET active_task_key=NULL,
                active_lease_until=NULL,active_lease_token=NULL WHERE active_task_key LIKE 'world:stale:%'"""))
            db.execute(text("DELETE FROM map_import_tasks WHERE region='auto:BE:900'"))
            db.execute(text("DELETE FROM map_import_tasks WHERE region=:region OR region LIKE :prefix"), {
                "region": self.unrelated_region, "prefix": self.unrelated_region + "-%",
            })
            db.execute(text("DELETE FROM map_datasets WHERE region='auto:BE:900'"))
            db.execute(text("DELETE FROM map_datasets WHERE region=:region OR region LIKE :prefix"), {
                "region": self.unrelated_region, "prefix": self.unrelated_region + "-%",
            })

    def _restore_environment(self):
        if self.previous_provider is None:
            os.environ.pop("CITY_GEOGRAPHY_PROVIDER", None)
        else:
            os.environ["CITY_GEOGRAPHY_PROVIDER"] = self.previous_provider
        if self.previous_storage is None:
            os.environ.pop("MAP_IMPORT_STORAGE_ROOT", None)
        else:
            os.environ["MAP_IMPORT_STORAGE_ROOT"] = self.previous_storage

    def _set_policy_request(self, country, state='unsupported', old_levels=None):
        with self.engine.begin() as db:
            db.execute(text("""UPDATE activity_preparation_requests SET state=:state,phase='done',
                country_codes=CAST(:countries AS jsonb),country_cursor=:country,
                candidate_cursor=17,unsupported_sample_count=2,safe_error='city_provider_unavailable'
                WHERE account_id=:account AND source_id=:source"""), {
                "state": state, "countries": json.dumps([country]), "country": country,
                "account": self.account, "source": self.source_id,
            })
            db.execute(text("UPDATE activities SET processed=TRUE WHERE id=:id"), {"id": self.activity_id})
            if old_levels is not None:
                profile, query = _provider_versions()
                key = f"country:{country}:{profile}:{','.join(map(str, old_levels))}:{query}"
                db.execute(text("""INSERT INTO city_public_tasks(task_key,task_kind,country_code,admin_levels)
                    VALUES (:key,'country',:country,CAST(:levels AS jsonb)) ON CONFLICT DO NOTHING"""), {
                    "key": key, "country": country, "levels": json.dumps(old_levels),
                })
                db.execute(text("""INSERT INTO activity_preparation_countries
                    (account_id,source_id,source_revision,country_code,task_key,state,candidate_cursor)
                    VALUES (:account,:source,1,:country,:key,'done',12)"""), {
                    "account": self.account, "source": self.source_id, "country": country, "key": key,
                })
        return key if old_levels is not None else None

    def test_new_country_policy_recovers_retained_unsupported_without_reupload(self):
        self._set_policy_request('ZZ')
        client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
        self.assertEqual(client.get('/api/coverage-preparation').json()['state'], 'unsupported')
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"ZZ":[7]}'}, clear=False):
            status = client.get('/api/coverage-preparation').json()
            self.assertEqual((status['state'], status['pending_sources'], status['unsupported_sources']),
                             ('pending', 1, 0))
            self.assertFalse(client.get(f'/api/activities/{self.activity_id}').json()['processed'])
            self.assertFalse(client.get('/api/activities').json()['items'][0]['processed'])
            self.assertTrue(_enroll_existing_sources(self.engine))
            self.assertFalse(_enroll_existing_sources(self.engine))
        with self.engine.connect() as db:
            request = db.execute(text("""SELECT state,phase,country_codes,country_cursor,candidate_cursor,
                unsupported_sample_count,safe_error FROM activity_preparation_requests
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).one()
            self.assertEqual(tuple(request), ('pending', 'catalog', [], None, None, None, None))
            original = db.execute(text("SELECT tracks,timestamps,processed FROM activities WHERE id=:id"),
                                  {"id": self.activity_id}).one()
            self.assertEqual(tuple(original), ([[[0.002, 0.001], [0.004, 0.001]]], [[None, None]], False))
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE account_id=:account"),
                                       {"account": self.account}).scalar_one(), 1)

    def test_same_level_selector_upgrade_recovers_old_country_links_without_changing_legacy_keys(self):
        old_key = self._set_policy_request('GB', old_levels=[6, 8])
        try:
            self.assertEqual(_country_task_key('BE', [8]),
                             'country:BE:municipal-admin-v1:8:public-scope-v1')
            self.assertEqual(_country_task_key('GB', [6, 8]), old_key + ':gb-local-authority-v1')
            client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
            self.assertEqual(client.get('/api/coverage-preparation').json()['state'], 'pending')
            self.assertTrue(_enroll_existing_sources(self.engine))
            self.assertFalse(_enroll_existing_sources(self.engine))
            with self.engine.connect() as db:
                self.assertEqual(db.execute(text('SELECT selector_profile FROM city_public_tasks WHERE task_key=:key'),
                                           {'key': old_key}).scalar_one(), 'admin-levels-v1')
                self.assertEqual(db.execute(text('SELECT count(*) FROM activity_preparation_countries WHERE account_id=:account'),
                                           {'account': self.account}).scalar_one(), 0)
        finally:
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM activity_preparation_countries WHERE account_id=:account'), {'account': self.account})
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key=:key'), {'key': old_key})

    def test_captured_country_selector_and_levels_survive_restart_and_policy_upgrade(self):
        self._assert_captured_country_selector('CA', [6, 8], 'ca-municipal-v1', [8])
        self._assert_captured_country_selector('AZ', [8], 'az-municipal-v1', [8])

    def _assert_captured_country_selector(self, country, levels, profile, current_levels):
        key = f'country:{country}:captured-' + uuid.uuid4().hex
        token = uuid.uuid4().hex
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO city_public_tasks
                (task_key,task_kind,country_code,admin_levels,selector_profile,status,attempts,lease_token,leased_until)
                VALUES (:key,'country',:country,CAST(:levels AS jsonb),:profile,'running',1,:token,
                    clock_timestamp()+interval '5 minutes')"""), {
                'key': key, 'token': token, 'country': country, 'levels': json.dumps(levels), 'profile': profile})
        calls = []
        class CapturedProvider:
            def fetch_country_catalog(self, country, levels, **kwargs):
                calls.append((country, levels, kwargs))
                return {'source_timestamp': '2026-10-13T00:00:00Z', 'cities': []}
        restarted = create_engine(DATABASE_URL)
        try:
            with patch('backend.app.city_geography_provider.get_city_geography_provider', return_value=CapturedProvider()), patch(
                    'backend.app.city_geography_provider.country_selector_profile', return_value=profile.replace('v1', 'v2')), patch.dict(
                    os.environ, {'CITY_ADMIN_LEVELS': json.dumps({country: current_levels})}, clear=False):
                with restarted.connect() as db:
                    persisted = dict(db.execute(text('SELECT * FROM city_public_tasks WHERE task_key=:key'),
                                                {'key': key}).mappings().one())
                _perform_public_request(restarted, persisted)
            self.assertEqual(calls, [(country, levels, {'selector_profile': profile})])
            with restarted.connect() as db:
                self.assertEqual(db.execute(text('SELECT status FROM city_public_tasks WHERE task_key=:key'),
                                            {'key': key}).scalar_one(), 'succeeded')
        finally:
            restarted.dispose()
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key=:key'), {'key': key})

    def test_azerbaijan_same_level_guard_recovers_legacy_links_without_reupload(self):
        old_key = self._set_policy_request('AZ', old_levels=[8])
        try:
            self.assertEqual(_country_task_key('AZ', [8]), old_key + ':az-municipal-v1')
            client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
            self.assertEqual(client.get('/api/coverage-preparation').json()['state'], 'pending')
            self.assertTrue(_enroll_existing_sources(self.engine))
            with self.engine.connect() as db:
                original = db.execute(text('SELECT tracks,timestamps FROM activities WHERE id=:id'),
                                      {'id': self.activity_id}).one()
                self.assertEqual(tuple(original), ([[[0.002, 0.001], [0.004, 0.001]]], [[None, None]]))
                self.assertEqual(db.execute(text('SELECT count(*) FROM activity_sources WHERE account_id=:account'),
                                           {'account': self.account}).scalar_one(), 1)
                self.assertEqual(db.execute(text('SELECT count(*) FROM activity_preparation_countries WHERE account_id=:account'),
                                           {'account': self.account}).scalar_one(), 0)
        finally:
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key=:key'), {'key': old_key})

    def test_legacy_country_worker_keeps_original_two_argument_provider_call(self):
        key = 'country:BE:legacy-' + uuid.uuid4().hex
        token = uuid.uuid4().hex
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO city_public_tasks
                (task_key,task_kind,country_code,admin_levels,status,attempts,lease_token,leased_until)
                VALUES (:key,'country','BE','[8]'::jsonb,'running',1,:token,clock_timestamp()+interval '5 minutes')"""),
                {'key': key, 'token': token})
        calls = []
        class LegacyProvider:
            def fetch_country_catalog(self, country, levels):
                calls.append((country, levels))
                return {'source_timestamp': '2026-10-13T00:00:00Z', 'cities': []}
        try:
            with self.engine.connect() as db:
                task = dict(db.execute(text('SELECT * FROM city_public_tasks WHERE task_key=:key'), {'key': key}).mappings().one())
            with patch('backend.app.city_geography_provider.get_city_geography_provider', return_value=LegacyProvider()):
                _perform_public_request(self.engine, task)
            self.assertEqual(calls, [('BE', [8])])
            with self.engine.connect() as db:
                self.assertEqual(db.execute(text('SELECT status FROM city_public_tasks WHERE task_key=:key'), {'key': key}).scalar_one(), 'succeeded')
        finally:
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key=:key'), {'key': key})

    def test_changed_levels_rebind_pending_request_and_preserve_shared_tasks(self):
        old_key = self._set_policy_request('BE', state='pending', old_levels=[8])
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"BE":[7]}'}, clear=False):
            self.assertTrue(_enroll_existing_sources(self.engine))
            with self.engine.connect() as db:
                self.assertEqual(db.execute(text("""SELECT count(*) FROM activity_preparation_countries
                    WHERE account_id=:account"""), {"account": self.account}).scalar_one(), 0)
                self.assertEqual(db.execute(text("SELECT count(*) FROM city_public_tasks WHERE task_key=:key"),
                                           {"key": old_key}).scalar_one(), 1)
            self.assertFalse(_enroll_existing_sources(self.engine))
            with self.engine.begin() as db:
                _enqueue_country_page(db, self.account, self.source_id, 1, ['BE'], None)
                scope = db.execute(text("""SELECT task_key,candidate_cursor,state
                    FROM activity_preparation_countries WHERE account_id=:account"""), {
                    "account": self.account,
                }).one()
                self.assertEqual(tuple(scope), (old_key.replace(':8:', ':7:'), None, 'pending'))
                db.execute(text('DELETE FROM activity_preparation_countries WHERE account_id=:account'),
                           {'account': self.account})
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key=:key'), {'key': scope.task_key})

    def test_pending_country_pagination_does_not_reset_missing_supported_links(self):
        self._set_policy_request('BE', state='pending')
        self.assertFalse(_enroll_existing_sources(self.engine))

    def test_failed_first_country_page_is_not_masked_by_unvisited_supported_countries(self):
        from backend.app.city_geography_provider import country_admin_levels
        profile = 'country-policy-failed-' + uuid.uuid4().hex
        countries = sorted(country_admin_levels())[:21]
        client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
        try:
            with patch('backend.app.city_geography_provider.PROFILE_VERSION', profile):
                with self.engine.begin() as db:
                    cursor = _enqueue_country_page(db, self.account, self.source_id, 1, countries, None)
                    db.execute(text("""UPDATE activity_preparation_requests SET state='failed',phase='done',
                        country_codes=CAST(:countries AS jsonb),country_cursor=:cursor,safe_error='city_provider_unavailable'
                        WHERE account_id=:account"""), {
                        'countries': json.dumps(countries), 'cursor': cursor, 'account': self.account,
                    })
                    db.execute(text("""UPDATE city_public_tasks SET status='failed',safe_error='city_provider_unavailable'
                        WHERE task_key=(SELECT task_key FROM activity_preparation_countries
                            WHERE account_id=:account AND country_code=:country)"""), {
                        'account': self.account, 'country': countries[0],
                    })
                self.assertEqual(client.get('/api/coverage-preparation').json()['state'], 'failed')
                self.assertFalse(_enroll_existing_sources(self.engine))
                self.assertFalse(_enroll_existing_sources(self.engine))
                with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': json.dumps({countries[0]: [9]})}, clear=False):
                    self.assertEqual(client.get('/api/coverage-preparation').json()['state'], 'pending')
                    self.assertTrue(_enroll_existing_sources(self.engine))
                    self.assertFalse(_enroll_existing_sources(self.engine))
        finally:
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM activity_preparation_countries WHERE account_id=:account'), {'account': self.account})
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key LIKE :pattern'), {'pattern': '%:' + profile + '%'})

    def test_unchanged_terminal_policy_and_unknown_country_do_not_reenroll(self):
        self._set_policy_request('BE', old_levels=[8])
        self.assertFalse(_enroll_existing_sources(self.engine))
        with self.engine.begin() as db:
            db.execute(text("""UPDATE activity_preparation_requests SET country_codes='["ZZ"]'::jsonb
                WHERE account_id=:account"""), {"account": self.account})
        self.assertFalse(_enroll_existing_sources(self.engine))

    def test_concurrent_policy_recovery_is_owner_fenced_and_idempotent(self):
        self._set_policy_request('ZZ')
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"ZZ":[7]}'}, clear=False):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: _enroll_existing_sources(self.engine), range(2)))
            self.assertTrue(any(results))
            self.assertFalse(_enroll_existing_sources(self.engine))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT count(*) FROM activity_preparation_requests
                WHERE account_id=:account AND source_id=:source AND state='pending'"""), {
                "account": self.account, "source": self.source_id,
            }).scalar_one(), 1)

    def test_deleted_source_between_policy_selection_and_owner_lock_is_not_revived(self):
        self._set_policy_request('ZZ')
        from backend.app.owner_guard import owner_lock
        def delete_before_lock(db, account, **kwargs):
            with self.engine.begin() as deletion:
                deletion.execute(text("DELETE FROM activity_sources WHERE account_id=:account AND id=:source"),
                                 {"account": account, "source": self.source_id})
            owner_lock(db, account, **kwargs)
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"ZZ":[7]}'}, clear=False), patch(
                'backend.app.city_provisioning.owner_lock', side_effect=delete_before_lock):
            _enroll_existing_sources(self.engine)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT count(*) FROM activity_preparation_requests
                WHERE account_id=:account"""), {"account": self.account}).scalar_one(), 0)

    def test_source_revision_change_between_policy_selection_and_owner_lock_is_fenced(self):
        self._set_policy_request('ZZ')
        from backend.app.owner_guard import owner_lock
        def revise_before_lock(db, account, **kwargs):
            with self.engine.begin() as revision:
                revision.execute(text("UPDATE activity_sources SET revision=2 WHERE id=:source"),
                                 {"source": self.source_id})
            owner_lock(db, account, **kwargs)
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"ZZ":[7]}'}, clear=False), patch(
                'backend.app.city_provisioning.owner_lock', side_effect=revise_before_lock):
            _enroll_existing_sources(self.engine)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT state FROM activity_preparation_requests
                WHERE account_id=:account AND source_revision=1"""), {
                "account": self.account,
            }).scalar_one(), 'unsupported')
        self.assertTrue(_enroll_existing_sources(self.engine))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT state FROM activity_preparation_requests
                WHERE account_id=:account AND source_revision=2"""), {
                "account": self.account,
            }).scalar_one(), 'pending')

    def test_policy_recovery_waits_for_matcher_before_resetting_request(self):
        self._set_policy_request('ZZ')
        from backend.app.owner_guard import owner_lock
        attempted = threading.Event()
        recovery_pid = []
        def observe_lock(db, account, **kwargs):
            recovery_pid.append(db.execute(text('SELECT pg_backend_pid()')).scalar_one())
            attempted.set()
            owner_lock(db, account, **kwargs)
        with self.engine.begin() as db:
            db.execute(text('UPDATE activity_preparation_requests SET unsupported_sample_count=1 WHERE account_id=:account'),
                       {'account': self.account})
        with patch.dict(os.environ, {'CITY_ADMIN_LEVELS': '{"ZZ":[7]}'}, clear=False), patch(
                'backend.app.city_provisioning.owner_lock', side_effect=observe_lock), ThreadPoolExecutor(max_workers=1) as pool:
            with self.engine.begin() as matching:
                owner_lock(matching, self.account)
                matching.execute(text("SELECT id FROM activities WHERE id=:id FOR UPDATE"), {'id': self.activity_id})
                recovery = pool.submit(_enroll_existing_sources, self.engine)
                self.assertTrue(attempted.wait(5))
                deadline = time.monotonic() + 5
                waiting = False
                while time.monotonic() < deadline and not recovery.done():
                    with self.engine.connect() as observer:
                        waiting = observer.execute(text("SELECT wait_event='advisory' FROM pg_stat_activity WHERE pid=:pid"),
                                                   {'pid': recovery_pid[0]}).scalar_one_or_none()
                    if waiting:
                        break
                    time.sleep(.01)
                self.assertTrue(waiting, 'Recovery must wait before taking request/activity row locks')
                _refresh_activity_processing(matching, account_id=self.account, source_id=self.source_id,
                                             source_revision=1, activity_id=self.activity_id)
            self.assertTrue(recovery.result(timeout=5))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text('SELECT state FROM activity_preparation_requests WHERE account_id=:account'),
                                       {'account': self.account}).scalar_one(), 'pending')

    def test_multicountry_discovery_uses_local_original_samples_and_distinct_municipal_levels(self):
        profile = 'country-policy-' + uuid.uuid4().hex
        tracks = [[[0.002, 0.001]], [[4.002, 0.001]]]
        timestamps = [['2026-10-13T07:00:00Z'], ['2026-10-13T08:00:00Z']]
        calls = []
        with self.engine.begin() as db:
            admission = db.execute(text('SELECT requests_reserved,bytes_reserved FROM city_provider_admission WHERE singleton=TRUE')).one()
            db.execute(text('UPDATE city_provider_admission SET requests_reserved=0,bytes_reserved=0 WHERE singleton=TRUE'))

        class MultiCountryProvider:
            def fetch_world_catalog(self):
                return {'regions': [{'country_code': country, 'geometry': {
                    'type': 'Polygon', 'coordinates': [[[west, -1], [west + 2, -1],
                        [west + 2, 1], [west, 1], [west, -1]]],
                }} for country, west in [('BR', -1), ('US', 1), ('JP', 3)]]}

            def fetch_country_catalog(self, country, levels):
                calls.append((country, levels))
                relation, west = {'BR': (900, 0), 'JP': (902, 4)}[country]
                return {'source_timestamp': '2026-10-13T00:00:00+00:00', 'cities': [{
                    'relation_id': relation, 'name': country + ' Municipality',
                    'bounds': [west, 0, west + 0.01, 0.01],
                }]}

            def fetch_city_extract(self, relation_id):
                from xml.etree.ElementTree import fromstring, tostring
                root = fromstring(_city_fixture())
                if relation_id == 902:
                    for node in root.findall('node'):
                        node.set('lon', str(float(node.get('lon')) + 4))
                    root.find('relation').set('id', '902')
                    root.find("relation/tag[@k='admin_level']").set('v', '7')
                return {'source_timestamp': '2026-10-13T00:00:00+00:00', 'xml': tostring(root)}

        try:
            with self.engine.begin() as db:
                db.execute(text("UPDATE activities SET tracks=CAST(:tracks AS json),timestamps=CAST(:times AS json) WHERE id=:id"),
                           {'tracks': json.dumps(tracks), 'times': json.dumps(timestamps), 'id': self.activity_id})
            with patch('backend.app.city_geography_provider.PROFILE_VERSION', profile), patch(
                    'backend.app.city_geography_provider.get_city_geography_provider', return_value=MultiCountryProvider()):
                with self.engine.begin() as db:
                    enqueue_preparation_request(db, account_id=self.account, source_id=self.source_id, source_revision=1)
                for _ in range(80):
                    with self.engine.begin() as db:
                        db.execute(text("UPDATE city_provider_admission SET next_request_at=clock_timestamp()-interval '1 second' WHERE singleton=TRUE"))
                    process_city_provisioning_step(self.engine)
                    process_map_task_step(self.engine)
                    with self.engine.connect() as db:
                        state = db.execute(text("SELECT state FROM activity_preparation_requests WHERE account_id=:account"),
                                           {'account': self.account}).scalar_one()
                    if state in ('ready', 'unsupported', 'failed'):
                        break
                self.assertEqual(state, 'ready')
                self.assertEqual(calls, [('BR', [8]), ('JP', [7])])
                self.assertFalse(_enroll_existing_sources(self.engine))
                with self.engine.connect() as db:
                    self.assertEqual(db.execute(text("SELECT tracks,timestamps FROM activities WHERE id=:id"),
                                                {'id': self.activity_id}).one(), (tracks, timestamps))
                    self.assertEqual(db.execute(text("SELECT count(*) FROM activity_preparation_cities WHERE account_id=:account AND touched=TRUE"),
                                                {'account': self.account}).scalar_one(), 2)
        finally:
            with self.engine.begin() as db:
                db.execute(text('DELETE FROM activity_preparation_requests WHERE account_id=:account'), {'account': self.account})
                db.execute(text('DELETE FROM city_public_tasks WHERE task_key LIKE :pattern'), {'pattern': '%:' + profile + '%'})
                db.execute(text("DELETE FROM map_import_tasks WHERE region IN ('auto:BR:900','auto:JP:902')"))
                db.execute(text("DELETE FROM map_datasets WHERE region IN ('auto:BR:900','auto:JP:902')"))
                db.execute(text('UPDATE city_provider_admission SET requests_reserved=:requests,bytes_reserved=:bytes WHERE singleton=TRUE'),
                           {'requests': admission.requests_reserved, 'bytes': admission.bytes_reserved})

    def test_worker_discovers_imports_and_verifies_city_from_original_samples(self):
        app = create_app(self.engine, identity_resolver=lambda: self.account)
        client = TestClient(app)
        response = client.get("/api/coverage-preparation")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertEqual(response.json()["pending_sources"], 1)

        provider = _SyntheticProvider()
        with patch("backend.app.city_geography_provider.get_city_geography_provider", return_value=provider):
            for _ in range(40):
                with self.engine.begin() as db:
                    db.execute(text("""UPDATE city_provider_admission SET next_request_at=
                        clock_timestamp()-interval '1 second' WHERE singleton=TRUE"""))
                worked = process_city_provisioning_step(self.engine)
                if not worked:
                    process_map_task_step(self.engine)
                with self.engine.connect() as db:
                    state = db.execute(text("""SELECT state FROM activity_preparation_requests
                        WHERE account_id=:account AND source_id=:source AND source_revision=1"""), {
                        "account": self.account, "source": self.source_id,
                    }).scalar_one()
                if state in {"ready", "unsupported", "failed"}:
                    break
        if state != "ready":
            with self.engine.connect() as db:
                diagnostic = {
                    "request": dict(db.execute(text("""SELECT * FROM activity_preparation_requests
                        WHERE account_id=:account AND source_id=:source"""), {
                        "account": self.account, "source": self.source_id,
                    }).mappings().one()),
                    "countries": [dict(row) for row in db.execute(text("""SELECT scope.*,task.status,task.safe_error,
                        task.result_json FROM activity_preparation_countries scope JOIN city_public_tasks task USING(task_key)
                        WHERE scope.account_id=:account AND scope.source_id=:source"""), {
                        "account": self.account, "source": self.source_id,
                    }).mappings().all()],
                    "cities": [dict(row) for row in db.execute(text("""SELECT city.*,task.status,task.safe_error,
                        task.map_import_task_id,map_task.status AS import_status,map_task.dataset_id
                        FROM activity_preparation_cities city JOIN city_public_tasks task USING(task_key)
                        LEFT JOIN map_import_tasks map_task ON map_task.id=task.map_import_task_id
                        WHERE city.account_id=:account AND city.source_id=:source"""), {
                        "account": self.account, "source": self.source_id,
                    }).mappings().all()],
                }
            self.fail(f"expected ready, got {state}: {diagnostic!r}")
        with self.engine.connect() as db:
            city = db.execute(text("""SELECT relation_id,touched,state FROM activity_preparation_cities
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).mappings().one()
            self.assertEqual((city["relation_id"], city["touched"], city["state"]), (900, True, "ready"))
            tasks = db.execute(text("""SELECT count(*) FROM city_public_tasks
                WHERE task_key IN ('world:municipal-admin-v1',
                    'country:BE:municipal-admin-v1:8:public-scope-v1',
                    'city:900:municipal-admin-v1:public-scope-v1:named-highway-city-name-v1')
                  AND status='succeeded'""")).scalar_one()
        self.assertEqual(tasks, 3)
        result = client.get("/api/coverage-preparation").json()
        self.assertEqual((result["state"], result["pending_sources"], result["cities"][0]["state"]),
                         ("ready", 0, "ready"))
        catalog = client.get("/api/datasets?q=Map City")
        self.assertEqual(catalog.status_code, 200)
        dataset_item = catalog.json()["items"][0]
        self.assertEqual(dataset_item["region"], "Map City, BE")
        self.assertEqual(client.get(f"/api/datasets/{dataset_item['dataset_id']}").json()["region"],
                         "Map City, BE")
        self.assertEqual(client.get("/api/progress").json()["datasets"][0]["region"], "Map City, BE")
        with self.engine.begin() as db:
            dataset_id = db.execute(text("SELECT id FROM map_datasets WHERE region='auto:BE:900'")).scalar_one()
            self.assertEqual(_dataset_applicability(db, account_id=self.account, source_id=self.source_id,
                source_revision=1, dataset_id=int(dataset_id), points=[[-0.0001, 0.5]]), (True, False))
            unrelated_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic unrelated coverage fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990902,'Unrelated City','8',
                    ST_Multi(ST_MakeEnvelope(100,20,101,21,4326)))"""), {"dataset": unrelated_id})
            queued = queue_source_coverage(db, account_id=self.account, source_id=self.source_id,
                source_revision=1, activity_id=self.activity_id, dataset_ids=[int(unrelated_id)])
            self.assertEqual(queued["jobs"], [])
            unrelated_coverage = get_account_coverage(db, self.account, int(unrelated_id))
            self.assertEqual((unrelated_coverage["status"], unrelated_coverage["pending_sources"]),
                             ("ready", 0))

    def test_pending_public_dependency_does_not_starve_legacy_source_enrollment(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_preparation_requests WHERE account_id=:account"), {
                "account": self.account,
            })
        self.assertTrue(process_city_provisioning_step(self.engine))
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT count(*) FROM activity_preparation_requests
                WHERE account_id=:account AND source_id=:source AND state='pending'"""), {
                "account": self.account, "source": self.source_id,
            }).scalar_one(), 1)

    def test_two_owners_dedupe_shared_public_catalog_and_provider_failure_is_retryable(self):
        other = f"city-provision-other-{uuid.uuid4().hex}"
        other_activity = self.activity_id + 10_000_000
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:id)"), {"id": other})
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'Other owner','2026-10-13','run',FALSE,0,
                    CAST(:tracks AS json),CAST(:timestamps AS json))"""), {
                "id": other_activity, "account": other,
                "tracks": json.dumps([[[0.002, 0.001]]]), "timestamps": json.dumps([[None]]),
            })
            other_source = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'gpx','succeeded') RETURNING id"""), {
                "account": other, "activity": other_activity,
            }).scalar_one()
            enqueue_preparation_request(db, account_id=other, source_id=int(other_source), source_revision=1)
            key = "world:" + _provider_versions()[0]
            self.assertEqual(db.execute(text("SELECT count(*) FROM city_public_tasks WHERE task_key=:key"), {
                "key": key,
            }).scalar_one(), 1)
            db.execute(text("""UPDATE activity_preparation_requests SET state='failed'
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            })
            db.execute(text("""UPDATE city_public_tasks SET status='failed',attempts=5,
                safe_error='city_provider_unavailable' WHERE task_key=:key"""), {"key": key})
            retry_coverage_preparation(db, self.account)
            states = db.execute(text("""SELECT request.state,task.status,task.attempts
                FROM activity_preparation_requests request CROSS JOIN city_public_tasks task
                WHERE request.account_id=:account AND request.source_id=:source
                  AND task.task_key=:key"""), {
                "account": self.account, "source": self.source_id, "key": key,
            }).one()
            db.execute(text("DELETE FROM activities WHERE user_id=:account"), {"account": other})
            db.execute(text("DELETE FROM accounts WHERE id=:account"), {"account": other})
        self.assertEqual(tuple(states), ("pending", "queued", 0))

    def test_disabling_provider_reconciles_processed_flag_after_matching(self):
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic unrelated coverage fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990903,'Unrelated City','8',
                    ST_Multi(ST_MakeEnvelope(100,20,101,21,4326)))"""), {"dataset": dataset_id})
            db.execute(text("""INSERT INTO source_dataset_applicability
                (account_id,source_id,source_revision,dataset_id,applicable,touched)
                VALUES (:account,:source,1,:dataset,FALSE,FALSE)"""), {
                "account": self.account, "source": self.source_id, "dataset": dataset_id,
            })
            db.execute(text("""UPDATE activity_preparation_requests SET state='pending'
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            })
            db.execute(text("UPDATE activities SET processed=FALSE WHERE id=:id"), {"id": self.activity_id})
        os.environ["CITY_GEOGRAPHY_PROVIDER"] = ""
        self.assertTrue(process_city_provisioning_step(self.engine))
        with self.engine.connect() as db:
            processed = db.execute(text("SELECT processed FROM activities WHERE id=:id"), {
                "id": self.activity_id,
            }).scalar_one()
        self.assertTrue(processed)
        self.assertFalse(process_city_provisioning_step(self.engine))

    def test_disabled_reconciliation_waits_for_every_source_in_recording_group(self):
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic grouped readiness fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990999,'Grouped City','8',ST_Multi(ST_MakeEnvelope(
                    -0.01,-0.01,0.01,0.01,4326)))"""), {"dataset": dataset_id})
            second_source = int(db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'fit','succeeded') RETURNING id"""), {
                "account": self.account, "activity": self.activity_id,
            }).scalar_one())
            enqueue_preparation_request(db, account_id=self.account, source_id=second_source, source_revision=1)
            for source_id, state in ((self.source_id, "succeeded"), (second_source, "failed")):
                db.execute(text("""INSERT INTO source_dataset_applicability
                    (account_id,source_id,source_revision,dataset_id,applicable,touched)
                    VALUES (:account,:source,1,:dataset,TRUE,FALSE)"""), {
                    "account": self.account, "source": source_id, "dataset": dataset_id,
                })
                job_id = int(db.execute(text("""INSERT INTO jobs
                    (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,last_error)
                    VALUES (:account,'match_coverage',:key,
                        jsonb_build_object('source_id',CAST(:source AS text),'source_revision','1',
                            'dataset_id',CAST(:dataset AS text)),
                        :status,1,5,:error)
                    RETURNING id"""), {
                    "account": self.account, "key": f"source:{source_id}:r1:dataset:{dataset_id}",
                    "source": source_id, "dataset": dataset_id, "status": state,
                    "error": "internal_error" if state == "failed" else None,
                }).scalar_one())
                db.execute(text("""INSERT INTO coverage_source_runs
                    (account_id,source_id,source_revision,dataset_id,job_id,status)
                    VALUES (:account,:source,1,:dataset,:job,:status)"""), {
                    "account": self.account, "source": source_id, "dataset": dataset_id,
                    "job": job_id, "status": state,
                })
            db.execute(text("UPDATE activities SET processed=FALSE WHERE id=:activity"), {
                "activity": self.activity_id,
            })
        os.environ["CITY_GEOGRAPHY_PROVIDER"] = ""
        self.assertFalse(process_city_provisioning_step(self.engine))
        with self.engine.connect() as db:
            self.assertFalse(db.execute(text("SELECT processed FROM activities WHERE id=:activity"), {
                "activity": self.activity_id,
            }).scalar_one())

    def test_later_city_coverage_reconciles_terminal_unsupported_preparation(self):
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic later city fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990998,'Later City','8',ST_Multi(ST_MakeEnvelope(
                    -0.01,-0.01,0.01,0.01,4326)))"""), {"dataset": dataset_id})
            db.execute(text("""UPDATE activity_preparation_requests SET state='unsupported',phase='done',
                unsupported_sample_count=2 WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            })
            db.execute(text("""INSERT INTO source_dataset_applicability
                (account_id,source_id,source_revision,dataset_id,applicable,touched)
                VALUES (:account,:source,1,:dataset,TRUE,FALSE)"""), {
                "account": self.account, "source": self.source_id, "dataset": dataset_id,
            })
            job_id = int(db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts)
                VALUES (:account,'match_coverage',:key,
                    jsonb_build_object('source_id',CAST(:source AS text),'source_revision','1',
                        'dataset_id',CAST(:dataset AS text)),
                    'succeeded',1,5) RETURNING id"""), {
                "account": self.account, "key": f"source:{self.source_id}:r1:dataset:{dataset_id}",
                "source": self.source_id, "dataset": dataset_id,
            }).scalar_one())
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status)
                VALUES (:account,:source,1,:dataset,:job,'succeeded')"""), {
                "account": self.account, "source": self.source_id, "dataset": dataset_id, "job": job_id,
            })
            _refresh_activity_processing(db, account_id=self.account, source_id=self.source_id,
                source_revision=1, activity_id=self.activity_id)
            prep = db.execute(text("""SELECT state,unsupported_sample_count
                FROM activity_preparation_requests WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).one()
        self.assertEqual(tuple(prep), ("ready", 0))
        client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
        try:
            result = client.get("/api/coverage-preparation")
        finally:
            client.close()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["state"], "ready")

    def test_indexed_envelope_candidates_cover_antimeridian_and_high_latitude(self):
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic boundary fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary) VALUES
                (:dataset,990904,'Dateline','8',ST_Multi(ST_MakeEnvelope(179.99985,-0.001,180,0.001,4326))),
                (:dataset,990905,'High latitude','8',ST_Multi(ST_MakeEnvelope(0.0011,0,0.0015,80,4326))),
                (:dataset,990909,'Polar seam','8',ST_Multi(ST_MakeEnvelope(-170.001,89.9998,-170,89.99995,4326)))"""), {
                "dataset": dataset_id,
            })
            candidate_sql = track_may_touch_dataset_sql("activity.track_geometry", ":dataset")
            for lon, lat in ((-179.9999, 0.0), (0.0, 80.0), (170.0, 89.9999)):
                found = db.execute(text(f"""SELECT ({candidate_sql.replace('activity.track_geometry',
                    'ST_SetSRID(ST_MakePoint(:lon,:lat),4326)')})"""), {
                    "lon": lon, "lat": lat, "dataset": dataset_id,
                }).scalar_one()
                self.assertTrue(found, (lon, lat))
            checks = (
                ([-179.99995, 0.0], True),  # about 22 m across the antimeridian
                ([0.0, 80.0], True),        # about 21 m beside a city spanning 0 to 80 degrees
                ([170.0, 89.9999], True),   # roughly 4 m at the polar seam
                ([-179.99, 0.0], False),    # roughly 1 km away
            )
            for point, expected in checks:
                applicable, touched = _dataset_applicability(
                    db, account_id=self.account, source_id=self.source_id, source_revision=1,
                    dataset_id=int(dataset_id), points=[point],
                )
                self.assertEqual((applicable, touched), (expected, False), point)

    def test_unrelated_import_backfill_skips_tracks_but_advances_cursor(self):
        from backend.app.map_imports import _advance_backfill_source, _release_backfill_task

        lease_token = uuid.uuid4().hex
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic unrelated coverage fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990906,'Far City','8',
                    ST_Multi(ST_MakeEnvelope(100,20,101,21,4326)))"""), {"dataset": dataset_id})
            task_id = db.execute(text("""INSERT INTO map_import_tasks
                (task_type,phase,region,dataset_id,status,source_cursor,lease_token,leased_until)
                VALUES ('coverage_backfill','backfill',:region,:dataset,'running',:cursor,:token,
                    clock_timestamp()+interval '5 minutes') RETURNING id"""), {
                "region": self.unrelated_region, "dataset": dataset_id,
                "cursor": self.source_id - 1, "token": lease_token,
            }).scalar_one()
        self.assertTrue(_advance_backfill_source(self.engine, int(task_id), lease_token))
        _release_backfill_task(self.engine, int(task_id), lease_token)
        with self.engine.connect() as db:
            task = db.execute(text("SELECT status,source_cursor FROM map_import_tasks WHERE id=:id"), {
                "id": task_id,
            }).mappings().one()
            runs = db.execute(text("SELECT count(*) FROM coverage_source_runs WHERE source_id=:source AND dataset_id=:dataset"), {
                "source": self.source_id, "dataset": dataset_id,
            }).scalar_one()
        self.assertEqual((task["status"], task["source_cursor"]), ("backfilling", self.source_id))
        self.assertEqual(runs, 0)

    def test_global_source_queue_only_persists_indexed_spatial_candidates(self):
        near_id = None
        with self.engine.begin() as db:
            for index in range(33):
                region = f"{self.unrelated_region}-{index}"
                dataset_id = db.execute(text("""INSERT INTO map_datasets
                    (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                     coverage_mode,coverage_evidence,status)
                    VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                        'Synthetic candidate fixture','active') RETURNING id"""), {
                    "region": region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                }).scalar_one()
                bounds = [-0.01, -0.01, 0.01, 0.01] if index == 0 else [100, 20, 101, 21]
                db.execute(text("""INSERT INTO cities
                    (dataset_id,osm_relation_id,name,admin_level,boundary)
                    VALUES (:dataset,:relation,'Candidate City','8',ST_Multi(ST_MakeEnvelope(
                        :west,:south,:east,:north,4326)))"""), {
                    "dataset": dataset_id, "relation": 991000 + index,
                    "west": bounds[0], "south": bounds[1], "east": bounds[2], "north": bounds[3],
                })
                if index == 0:
                    near_id = int(dataset_id)
        with self.engine.begin() as db:
            queued = queue_source_coverage(db, account_id=self.account, source_id=self.source_id,
                source_revision=1, activity_id=self.activity_id)
            stored = db.execute(text("""SELECT count(*) FROM source_dataset_applicability
                WHERE account_id=:account AND source_id=:source AND source_revision=1"""), {
                "account": self.account, "source": self.source_id,
            }).scalar_one()
        self.assertEqual(queued["dataset_ids"], [near_id])
        self.assertEqual(len(queued["jobs"]), 1)
        self.assertEqual(stored, 1)

    def test_retry_and_failure_feed_ignore_irrelevant_active_city_work(self):
        far_id = None
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts)
                VALUES (:account,'process_upload',:key,'{}'::jsonb,'succeeded',1,5)"""), {
                "account": self.account, "key": f"upload:{self.source_id}:r1",
            })
            for index in range(34):
                dataset_id = db.execute(text("""INSERT INTO map_datasets
                    (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                     coverage_mode,coverage_evidence,status)
                    VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                        'Synthetic retry candidate fixture','active') RETURNING id"""), {
                    "region": f"{self.unrelated_region}-retry-{index}",
                    "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                }).scalar_one()
                bounds = [-0.01, -0.01, 0.01, 0.01] if index == 0 else [100, 20, 101, 21]
                db.execute(text("""INSERT INTO cities
                    (dataset_id,osm_relation_id,name,admin_level,boundary)
                    VALUES (:dataset,:relation,'Retry Candidate','8',ST_Multi(ST_MakeEnvelope(
                        :west,:south,:east,:north,4326)))"""), {
                    "dataset": dataset_id, "relation": 992000 + index,
                    "west": bounds[0], "south": bounds[1], "east": bounds[2], "north": bounds[3],
                })
                if index == 1:
                    far_id = int(dataset_id)
            failed_job = int(db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,last_error)
                VALUES (:account,'match_coverage',:key,
                    jsonb_build_object('source_id',CAST(:source AS text),'source_revision','1',
                        'dataset_id',CAST(:dataset AS text)),
                    'failed',1,5,'internal_error') RETURNING id"""), {
                "account": self.account, "key": f"source:{self.source_id}:r1:dataset:{far_id}",
                "source": self.source_id, "dataset": far_id,
            }).scalar_one())
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,last_error)
                VALUES (:account,:source,1,:dataset,:job,'failed','internal_error')"""), {
                "account": self.account, "source": self.source_id, "dataset": far_id, "job": failed_job,
            })
        client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
        try:
            failures = client.get("/api/sync/failures")
            retry = client.post(f"/api/uploads/{self.source_id}/retry-coverage")
        finally:
            client.close()
        self.assertEqual(failures.status_code, 200)
        self.assertEqual(failures.json()["items"], [])
        self.assertEqual(retry.status_code, 204)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": failed_job,
            }).scalar_one(), "failed")

    def test_distant_activation_stays_ready_across_sync_progress_and_street_apis(self):
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic distant dataset','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            city_id = db.execute(text("""INSERT INTO cities
                (dataset_id,osm_relation_id,name,admin_level,boundary)
                VALUES (:dataset,990908,'Distant City','8',
                    ST_Multi(ST_MakeEnvelope(100,20,101,21,4326))) RETURNING id"""), {
                "dataset": dataset_id,
            }).scalar_one()
            db.execute(text("""INSERT INTO streets
                (dataset_id,city_id,normalized_name,display_name,eligible_node_count)
                VALUES (:dataset,:city,'far-road','Far Road',1)"""), {
                "dataset": dataset_id, "city": city_id,
            })
            db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts)
                VALUES (:account,'process_upload',:key,'{}'::jsonb,'succeeded',1,5)"""), {
                "account": self.account, "key": f"upload:{self.source_id}:r1",
            })
            db.execute(text("""UPDATE activity_preparation_requests SET state='pending'
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            })
            db.execute(text("UPDATE activities SET processed=FALSE WHERE id=:activity"), {
                "activity": self.activity_id,
            })
        os.environ["CITY_GEOGRAPHY_PROVIDER"] = ""
        self.assertTrue(process_city_provisioning_step(self.engine))
        client = TestClient(create_app(self.engine, identity_resolver=lambda: self.account))
        try:
            sync = client.get("/api/sync/status").json()
            self.assertEqual(sync["coverage"], {"ready": 0, "pending": 0, "failed": 0, "unavailable": 1})
            detail = client.get(f"/api/datasets/{dataset_id}").json()
            self.assertEqual(detail["region"], self.unrelated_region)
            cities = client.get("/api/cities", params={"dataset_id": dataset_id}).json()
            self.assertEqual(cities["coverage"]["status"], "ready")
            streets = client.get(f"/api/cities/{city_id}/streets",
                                 params={"dataset_id": dataset_id}).json()
            self.assertEqual(streets["coverage"]["status"], "ready")
            progress = client.get("/api/progress").json()
            match = next(item for item in progress["datasets"] if item["dataset_id"] == str(dataset_id))
            self.assertEqual(match["state"], "ready")
            activity = client.get(f"/api/activities/{self.activity_id}").json()
            self.assertTrue(activity["processed"])
        finally:
            client.close()

    def test_retry_preserves_failed_city_backfill_phase_and_cursor(self):
        key = f"city:990907:{uuid.uuid4().hex[:8]}"
        with self.engine.begin() as db:
            dataset_id = db.execute(text("""INSERT INTO map_datasets
                (region,source_timestamp,source_checksum,eligibility_rule_version,selection_key,
                 coverage_mode,coverage_evidence,status)
                VALUES (:region,clock_timestamp(),:checksum,'city-provision-test','selection','complete',
                    'Synthetic retry fixture','active') RETURNING id"""), {
                "region": self.unrelated_region, "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            }).scalar_one()
            task_id = db.execute(text("""INSERT INTO map_import_tasks
                (task_type,phase,region,object_key,source_checksum,city_relation_ids,
                 eligibility_rule_version,selection_key,coverage_mode,coverage_evidence,
                 dataset_id,status,source_cursor,last_error)
                VALUES ('osm_import','backfill',:region,:object_key,:checksum,CAST('[990907]' AS jsonb),
                    'test-rule',:selection,'complete','synthetic',:dataset,'failed',:cursor,'synthetic')
                RETURNING id"""), {
                "region": self.unrelated_region, "object_key": uuid.uuid4().hex[:32] + "-" + uuid.uuid4().hex[:12] + ".osm",
                "checksum": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                "selection": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                "dataset": dataset_id, "cursor": 445,
            }).scalar_one()
            db.execute(text("""INSERT INTO city_public_tasks
                (task_key,task_kind,country_code,relation_id,status,map_import_task_id,safe_error)
                VALUES (:key,'city','BE',990907,'failed',:task,'city_import_failed')"""), {
                "key": key, "task": task_id,
            })
            db.execute(text("""INSERT INTO activity_preparation_cities
                (account_id,source_id,source_revision,relation_id,name,task_key,dataset_id,state)
                VALUES (:account,:source,1,990907,'Retry City',:key,:dataset,'failed')"""), {
                "account": self.account, "source": self.source_id, "key": key, "dataset": dataset_id,
            })
            db.execute(text("""UPDATE activity_preparation_requests SET state='failed'
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            })
            retry_coverage_preparation(db, self.account)
            row = db.execute(text("SELECT phase,source_cursor,status FROM map_import_tasks WHERE id=:id"), {
                "id": task_id,
            }).mappings().one()
        self.assertEqual((row["phase"], row["source_cursor"], row["status"]),
                         ("backfill", 445, "backfilling"))

    def test_catalog_bbox_candidates_never_claim_actual_city_coverage(self):
        with self.engine.begin() as db:
            db.execute(text("""UPDATE activities SET tracks=CAST(:tracks AS json)
                WHERE id=:id AND user_id=:account"""), {
                "tracks": json.dumps([[[0.5, 0.5]]]), "id": self.activity_id, "account": self.account,
            })
        self.assertEqual(process_city_provisioning_step(self.engine), True)
        provider = _SyntheticProvider()
        with patch("backend.app.city_geography_provider.get_city_geography_provider", return_value=provider):
            for _ in range(12):
                with self.engine.begin() as db:
                    db.execute(text("""UPDATE city_provider_admission SET next_request_at=
                        clock_timestamp()-interval '1 second' WHERE singleton=TRUE"""))
                if not process_city_provisioning_step(self.engine):
                    break
        with self.engine.connect() as db:
            request = db.execute(text("""SELECT state,unsupported_sample_count FROM activity_preparation_requests
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).mappings().one()
        self.assertEqual(request["state"], "unsupported")

    def test_paused_track_envelope_does_not_request_cities_without_recorded_samples(self):
        with self.engine.begin() as db:
            db.execute(text("""UPDATE activities SET tracks=CAST(:tracks AS json),processed=FALSE
                WHERE id=:id AND user_id=:account"""), {
                "tracks": json.dumps([[[-0.5, 0.0]], [[0.5, 0.0]]]),
                "id": self.activity_id, "account": self.account,
            })
        self.assertTrue(process_city_provisioning_step(self.engine))
        provider = _SyntheticProvider()
        with patch("backend.app.city_geography_provider.get_city_geography_provider", return_value=provider):
            for _ in range(16):
                with self.engine.begin() as db:
                    db.execute(text("UPDATE city_provider_admission SET next_request_at=clock_timestamp()-interval '1 second' WHERE singleton=TRUE"))
                if not process_city_provisioning_step(self.engine):
                    break
        with self.engine.connect() as db:
            city_rows = db.execute(text("""SELECT count(*) FROM activity_preparation_cities
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).scalar_one()
            state = db.execute(text("""SELECT state FROM activity_preparation_requests
                WHERE account_id=:account AND source_id=:source"""), {
                "account": self.account, "source": self.source_id,
            }).scalar_one()
        self.assertEqual(city_rows, 0)
        self.assertEqual(state, "unsupported")

    def test_world_country_candidates_respect_holes_and_multipolygon_components(self):
        point = [[0.002, 0.001]]
        hole = {"country_code": "BE", "geometry": {
            "type": "Polygon", "coordinates": [
                [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]],
                [[0.001, 0.0], [0.003, 0.0], [0.003, 0.002], [0.001, 0.002], [0.001, 0.0]],
            ],
        }}
        polygon = {"country_code": "BE", "geometry": {
            "type": "Polygon", "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
        }}
        multipart = {"country_code": "NL", "geometry": {
            "type": "MultiPolygon", "coordinates": [
                [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]],
                [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
            ],
        }}
        with self.engine.connect() as db:
            self.assertEqual(_world_candidate_countries(db, [hole], point), [])
            self.assertEqual(_world_candidate_countries(db, [polygon], point), ["BE"])
            self.assertEqual(_world_candidate_countries(db, [multipart], point), ["NL"])

    def test_invalid_public_country_polygon_fails_closed(self):
        bow_tie = {"country_code": "BE", "geometry": {
            "type": "Polygon", "coordinates": [[
                [-1, -1], [1, 1], [-1, 1], [1, -1], [-1, -1],
            ]],
        }}
        with self.engine.connect() as db:
            with self.assertRaisesRegex(ValueError, "invalid_city_provider_response"):
                _world_candidate_countries(db, [bow_tie], [[0, 0]])

    def test_expired_city_public_lease_rolls_back_import_task_and_snapshot(self):
        relation = 8_000_000 + int(uuid.uuid4().hex[:6], 16)
        key = f"city:{relation}:municipal-admin-v1:public-scope-v1:named-highway-city-name-v1"
        token = uuid.uuid4().hex
        region = f"auto:BE:{relation}"
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO city_public_tasks
                (task_key,task_kind,country_code,relation_id,status,attempts,lease_token,leased_until)
                VALUES (:key,'city','BE',:relation,'running',1,:token,
                    clock_timestamp()-interval '1 second')"""), {
                "key": key, "relation": relation, "token": token,
            })
            db.execute(text("""UPDATE city_provider_admission SET active_task_key=:key,
                active_lease_until=clock_timestamp()-interval '1 second',active_lease_token=:token
                WHERE singleton=TRUE"""), {"key": key, "token": token})

        class ExpiredLeaseProvider(_SyntheticProvider):
            def fetch_city_extract(self, relation_id):
                return {"source_timestamp": "2026-10-13T00:00:00+00:00", "xml": _city_fixture()}

        task = {"task_key": key, "lease_token": token, "task_kind": "city",
                "country_code": "BE", "relation_id": relation, "max_bytes": 64 * 1024 * 1024}
        with patch("backend.app.city_geography_provider.get_city_geography_provider",
                   return_value=ExpiredLeaseProvider()):
            _perform_public_request(self.engine, task)
        with self.engine.connect() as db:
            count = db.execute(text("SELECT count(*) FROM map_import_tasks WHERE region=:region"), {
                "region": region,
            }).scalar_one()
            status = db.execute(text("SELECT status FROM city_public_tasks WHERE task_key=:key"), {
                "key": key,
            }).scalar_one()
        self.assertEqual(count, 0)
        self.assertEqual(status, "running")
        self.assertEqual(list(Path(os.environ["MAP_IMPORT_STORAGE_ROOT"]).iterdir()), [])

        stale_world_key, stale_world_token = f"world:stale:{uuid.uuid4().hex}", uuid.uuid4().hex
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO city_public_tasks
                (task_key,task_kind,status,attempts,lease_token,leased_until)
                VALUES (:key,'world','running',1,:token,clock_timestamp()-interval '1 second')"""), {
                "key": stale_world_key, "token": stale_world_token,
            })
            db.execute(text("""UPDATE city_provider_admission SET active_task_key=:key,
                active_lease_until=clock_timestamp()-interval '1 second',active_lease_token=:token
                WHERE singleton=TRUE"""), {"key": stale_world_key, "token": stale_world_token})
        _perform_public_request(self.engine, {
            "task_key": stale_world_key, "lease_token": stale_world_token, "task_kind": "world",
        })
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT status FROM city_public_tasks WHERE task_key=:key"), {
                "key": stale_world_key,
            }).scalar_one(), "running")
