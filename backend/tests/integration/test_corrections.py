import hashlib
import json
import os
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from backend.app.coverage import process_source_dataset
from backend.app.geography import import_osm_xml
from backend.app.jobs import claim
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.corrections import delete_activity, process_private_object_cleanup
from backend.app.coverage import queue_source_coverage
from backend.app.uploads import process_upload
from backend.tests.integration.test_explorer import LONG_NODES, TINY_NODES, _osm_fixture


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = re.compile(r"city_runner_test_[0-9a-f]{12}\Z")


@unittest.skipUnless(DATABASE_URL, "run with scripts/dev/postgis-test.sh")
class CorrectionsPostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("corrections tests require the disposable PostGIS database")
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = DATABASE_URL
        cls.engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        cls.config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url

    def setUp(self):
        self.region = f"corrections-{uuid.uuid4().hex}"
        self.account = f"corrections-a-{uuid.uuid4().hex}"
        self.other = f"corrections-b-{uuid.uuid4().hex}"
        self.tokens = {self.account: uuid.uuid4().hex * 2, self.other: uuid.uuid4().hex * 2}
        self.temp = tempfile.TemporaryDirectory()
        self.storage_temp = tempfile.TemporaryDirectory()
        self.storage_env = patch.dict(os.environ, {
            "UPLOAD_STORAGE_DIR": self.storage_temp.name,
        })
        self.storage_env.start()
        self.store = LocalObjectStore()
        self.osm = Path(self.temp.name) / "city.osm"
        self.osm.write_bytes(_osm_fixture())
        with self.engine.begin() as db:
            for account, token in self.tokens.items():
                db.execute(text("INSERT INTO accounts(id) VALUES (:account)"), {"account": account})
                db.execute(text("""INSERT INTO sessions(token_digest,account_id,expires_at)
                    VALUES (:digest,:account,:expires)"""), {
                    "digest": hashlib.sha256(token.encode()).hexdigest(), "account": account,
                    "expires": datetime.now(timezone.utc) + timedelta(days=1),
                })
        self.dataset = import_osm_xml(self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T00:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic complete correction fixture")["dataset_id"]
        self.client = TestClient(create_app(self.engine))
        with self.engine.connect() as db:
            self.city = int(db.execute(text("SELECT id FROM cities WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).scalar_one())
            self.streets = {row["display_name"]: int(row["id"]) for row in db.execute(text(
                "SELECT id,display_name FROM streets WHERE dataset_id=:dataset"), {
                "dataset": self.dataset,
            }).mappings()}

    def tearDown(self):
        self.client.close()
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM accounts WHERE id IN (:a,:b)"), {
                "a": self.account, "b": self.other,
            })
            db.execute(text("DELETE FROM map_datasets WHERE region=:region"), {"region": self.region})
        self.storage_env.stop()
        self.storage_temp.cleanup()
        self.temp.cleanup()

    def headers(self, account=None):
        account = account or self.account
        return {"Authorization": f"Bearer {self.tokens[account]}"}

    def add_activity_source(self, activity_id, node_ids, *, key=None, content_hash=None):
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (id,user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:id,:account,'fixture','2026-10-06','run',true,0,'[]'::json,'[]'::json)"""), {
                "id": activity_id, "account": self.account,
            })
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,content_hash,private_object_key,status)
                VALUES (:account,:activity,'gpx',:hash,:key,'succeeded') RETURNING id"""), {
                "account": self.account, "activity": activity_id, "hash": content_hash, "key": key,
            }).scalar_one()
            job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts)
                VALUES (:account,'match_coverage',:key,'{}'::json,'succeeded',1,5) RETURNING id"""), {
                "account": self.account, "key": f"match-{activity_id}",
            }).scalar_one()
            db.execute(text("""INSERT INTO coverage_source_runs
                (account_id,source_id,source_revision,dataset_id,job_id,status,matched_node_count)
                VALUES (:account,:source,1,:dataset,:job,'succeeded',:count)"""), {
                "account": self.account, "source": source_id, "dataset": self.dataset,
                "job": job_id, "count": len(node_ids),
            })
            for node_id in node_ids:
                db.execute(text("""INSERT INTO source_node_contributions
                    (account_id,source_id,source_revision,dataset_id,node_id)
                    VALUES (:account,:source,1,:dataset,:node)"""), {
                    "account": self.account, "source": source_id, "dataset": self.dataset, "node": node_id,
                })
        return int(source_id)

    def test_manual_completion_is_versioned_owner_only_and_additive(self):
        tiny_id = self.streets["Tiny Road"]
        url = f"/api/streets/{tiny_id}/manual-completion"
        put = self.client.put(url, params={"dataset_id": self.dataset}, json={"reason": "  verified in person  "},
                              headers=self.headers())
        self.assertEqual(put.status_code, 204, put.text)
        own = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                              headers=self.headers()).json()
        self.assertEqual((own["state"], own["effective_state"]), ("missing", "complete"))
        self.assertEqual((own["manual_completed"], own["manual_reason"]), (True, "verified in person"))
        self.assertEqual(own["remaining_nodes_page"]["total"], 3)
        before_repeat = own["coverage"]["progress_revision"]
        self.assertEqual(self.client.put(url, params={"dataset_id": self.dataset},
            json={"reason": "verified in person"}, headers=self.headers()).status_code, 204)
        own = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                              headers=self.headers()).json()
        self.assertEqual(own["coverage"]["progress_revision"], before_repeat)
        other = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                                headers=self.headers(self.other)).json()
        self.assertFalse(other["manual_completed"])
        self.assertIsNone(other["manual_reason"])
        city = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                               headers=self.headers()).json()["items"][0]
        self.assertEqual((city["completed_streets"], city["manual_completed_streets"],
                          city["effective_completed_streets"]), (0, 1, 1))
        progress = self.client.get("/api/progress", headers=self.headers()).json()["datasets"][0]
        self.assertEqual((progress["completed_streets"], progress["manual_completed_streets"],
                          progress["effective_completed_streets"]), (0, 1, 1))
        path = f"/api/cities/{self.city}/streets"
        completed = self.client.get(path, params={"dataset_id": self.dataset, "filter": "completed"},
                                    headers=self.headers()).json()
        self.assertEqual([item["name"] for item in completed["items"]], ["Tiny Road"])
        self.assertEqual(completed["items"][0]["state"], "missing")
        self.assertEqual([item["name"] for item in self.client.get(path,
            params={"dataset_id": self.dataset, "filter": "incomplete"},
            headers=self.headers()).json()["items"]], ["Long Road"])
        self.assertEqual(self.client.get(path, params={"dataset_id": self.dataset, "filter": "partial"},
            headers=self.headers()).json()["items"], [])
        map_result = self.client.get("/api/map", params={"bbox": "0,0,1,1", "zoom": 12},
                                     headers=self.headers()).json()
        feature = next(item for item in map_result["streets"] if item["street_id"] == str(tiny_id))
        self.assertFalse(feature["completed"])
        self.assertTrue(feature["effective_completed"])
        self.assertEqual(feature["manual_reason"], "verified in person")

        revision_before = int(own["coverage"]["progress_revision"])
        deleted = self.client.delete(url, params={"dataset_id": self.dataset}, headers=self.headers())
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.delete(url, params={"dataset_id": self.dataset},
                                            headers=self.headers()).status_code, 204)
        after = self.client.get(f"/api/streets/{tiny_id}", params={"dataset_id": self.dataset},
                                headers=self.headers()).json()
        self.assertFalse(after["manual_completed"])
        self.assertEqual(after["state"], "missing")
        self.assertGreater(int(after["coverage"]["progress_revision"]), revision_before)
        self.assertEqual(self.client.put(url, params={"dataset_id": self.dataset}, json={"reason": "   "},
            headers=self.headers()).status_code, 422)

    def test_manual_completion_remains_visible_during_pending_matching_and_versions_are_fenced(self):
        street_id = self.streets["Tiny Road"]
        url = f"/api/streets/{street_id}/manual-completion"
        self.assertEqual(self.client.put(url, params={"dataset_id": self.dataset}, json={"reason": "field checked"},
            headers=self.headers()).status_code, 204)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:account,'pending','2026-10-06','run',false,2,'[]'::json,'[]'::json)"""), {
                "account": self.account,
            })
            activity = db.execute(text("SELECT max(id) FROM activities WHERE user_id=:account"), {
                "account": self.account,
            }).scalar_one()
            db.execute(text("""INSERT INTO activity_sources(account_id,activity_id,source_kind,status)
                VALUES (:account,:activity,'gpx','succeeded')"""), {
                "account": self.account, "activity": activity,
            })
        pending = self.client.get(f"/api/streets/{street_id}", params={"dataset_id": self.dataset},
                                  headers=self.headers()).json()
        self.assertEqual(pending["coverage"]["status"], "pending")
        self.assertIsNone(pending["state"])
        self.assertTrue(pending["manual_completed"])
        self.assertEqual(pending["effective_state"], "complete")
        self.assertEqual(pending["manual_reason"], "field checked")
        self.assertIsNone(pending["remaining_nodes"])
        city = self.client.get("/api/cities", params={"dataset_id": self.dataset},
                               headers=self.headers()).json()["items"][0]
        self.assertIsNone(city["completed_streets"])
        self.assertEqual(city["manual_completed_streets"], 1)
        self.assertIsNone(city["effective_completed_streets"])
        progress = self.client.get("/api/progress", headers=self.headers()).json()["datasets"][0]
        self.assertIsNone(progress["completed_streets"])
        self.assertEqual(progress["manual_completed_streets"], 1)
        self.assertIsNone(progress["effective_completed_streets"])
        map_result = self.client.get("/api/map", params={"bbox": "0,0,1,1", "zoom": 12},
                                     headers=self.headers()).json()
        feature = next(item for item in map_result["streets"] if item["street_id"] == str(street_id))
        self.assertIsNone(feature["completed"])
        self.assertTrue(feature["effective_completed"])

        self.osm.write_bytes(_osm_fixture(revision="replacement"))
        staged = import_osm_xml(self.engine, self.osm, region=self.region, city_relation_ids=[900],
            source_timestamp="2026-10-06T01:00:00Z", coverage_mode="complete",
            coverage_evidence="Synthetic staged replacement")["dataset_id"]
        with self.engine.connect() as db:
            staged_street = int(db.execute(text("SELECT id FROM streets WHERE dataset_id=:id AND display_name='Tiny Road'"), {
                "id": staged,
            }).scalar_one())
        self.assertEqual(self.client.put(f"/api/streets/{staged_street}/manual-completion",
            params={"dataset_id": staged}, json={"reason": "old label"}, headers=self.headers()).status_code, 409)

    def test_delete_activity_cancels_jobs_fences_stale_worker_and_preserves_overlap(self):
        object_a = self.store.write(b"same source bytes")
        object_b = self.store.write(b"same source bytes")
        digest = hashlib.sha256(b"same source bytes").hexdigest()
        node = TINY_NODES[0]
        source_a = self.add_activity_source(900001, [node], key=object_a, content_hash=digest)
        source_b = self.add_activity_source(900002, [node], key=object_b)
        with self.engine.begin() as db:
            upload_job = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,status,attempts,max_attempts,lease_token,leased_until)
                VALUES (:account,'process_upload',:dedupe,:payload,'running',1,5,:token,
                    clock_timestamp()+interval '1 minute') RETURNING id"""), {
                "account": self.account, "dedupe": f"upload:{source_a}:r1",
                "payload": __import__("json").dumps({"source_id": source_a, "revision": 1}),
                "token": "1" * 32,
            }).scalar_one()
            match_job = db.execute(text("SELECT job_id FROM coverage_source_runs WHERE account_id=:account AND source_id=:source"), {
                "account": self.account, "source": source_a,
            }).scalar_one()
            db.execute(text("""UPDATE jobs SET status='running',attempts=1,lease_token=:token,
                leased_until=clock_timestamp()+interval '1 minute' WHERE id=:job"""), {
                "token": "2" * 32, "job": match_job,
            })
            stale = dict(db.execute(text("SELECT * FROM jobs WHERE id=:job"), {"job": match_job}).mappings().one())
            stale["payload"] = {"source_id": source_a, "source_revision": 1, "dataset_id": self.dataset}
        response = self.client.delete("/api/activities/900001", headers=self.headers())
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(self.client.delete("/api/activities/900001", headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.delete("/api/activities/900002", headers=self.headers(self.other)).status_code, 404)
        process_source_dataset(self.engine, stale)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM source_node_contributions WHERE account_id=:account AND source_id=:source"), {
                "account": self.account, "source": source_a,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM source_node_contributions WHERE account_id=:account AND source_id=:source"), {
                "account": self.account, "source": source_b,
            }).scalar_one(), 1)
            cancelled = db.execute(text("SELECT status,lease_token,leased_until FROM jobs WHERE id=ANY(:ids)"), {
                "ids": [upload_job, match_job],
            }).mappings().all()
            self.assertEqual(len(cancelled), 2)
            self.assertTrue(all(row["status"] == "cancelled" and row["lease_token"] is None for row in cancelled))
            cleanup = dict(db.execute(text("SELECT * FROM jobs WHERE kind='delete_private_object' AND account_id=:account"), {
                "account": self.account,
            }).mappings().one())
        # Source deletion releases the exact-content unique key; the new object key is independent.
        new_key = self.store.write(b"same source bytes")
        with self.engine.begin() as db:
            inserted = db.execute(text("""INSERT INTO activity_sources
                (account_id,source_kind,content_hash,private_object_key,status)
                VALUES (:account,'gpx',:hash,:key,'queued') RETURNING id"""), {
                "account": self.account, "hash": digest, "key": new_key,
            }).scalar_one()
        self.assertGreater(int(inserted), 0)
        self.assertTrue((Path(self.store.root) / object_a).exists())
        process_private_object_cleanup(cleanup, self.store)
        self.assertFalse((Path(self.store.root) / object_a).exists())
        self.assertTrue((Path(self.store.root) / object_b).exists())
        process_private_object_cleanup(cleanup, self.store)  # retry after deletion is already complete
        self.assertTrue((Path(self.store.root) / new_key).exists())

    def test_delete_during_upload_read_fences_worker_and_cleanup_is_durable(self):
        content = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
          <trk><name>Late upload</name><trkseg><trkpt lat="50" lon="4"><time>2026-10-06T08:00:00Z</time></trkpt>
          <trkpt lat="50.1" lon="4.1"><time>2026-10-06T08:01:00Z</time></trkpt></trkseg></trk></gpx>'''
        object_key = self.store.write(content)
        with self.engine.begin() as db:
            db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:account,'placeholder','2026-10-06','run',false,0,'[]'::json,'[]'::json)"""), {
                "account": self.account,
            })
            activity_id = db.execute(text("SELECT max(id) FROM activities WHERE user_id=:account"), {
                "account": self.account,
            }).scalar_one()
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,content_hash,private_object_key,status)
                VALUES (:account,:activity,'gpx',:hash,:key,'queued') RETURNING id"""), {
                "account": self.account, "activity": activity_id,
                "hash": hashlib.sha256(content).hexdigest(), "key": object_key,
            }).scalar_one()
            db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload,priority)
                VALUES (:account,'process_upload',:key,CAST(:payload AS jsonb),100)"""), {
                "account": self.account, "key": f"upload:{source_id}:r1",
                "payload": json.dumps({"source_id": source_id, "revision": 1}),
            })
        job = claim(self.engine, kind="process_upload")
        self.assertIsNotNone(job)
        read_started = threading.Event()
        release_read = threading.Event()
        original_read = self.store.read

        def delayed_read(key, **kwargs):
            data = original_read(key, **kwargs)
            read_started.set()
            if not release_read.wait(5):
                raise TimeoutError("test read release timed out")
            return data

        self.store.read = delayed_read
        worker_errors = []
        worker = threading.Thread(target=lambda: self._capture_error(
            worker_errors, lambda: process_upload(self.engine, job, self.store)), daemon=True)
        worker.start()
        self.assertTrue(read_started.wait(5))
        self.assertEqual(self.client.delete(f"/api/activities/{activity_id}", headers=self.headers()).status_code, 204)
        with self.engine.connect() as db:
            cleanup = dict(db.execute(text("""SELECT * FROM jobs WHERE account_id=:account
                AND kind='delete_private_object'"""), {"account": self.account}).mappings().one())
        process_private_object_cleanup(cleanup, self.store)
        self.assertFalse((Path(self.store.root) / object_key).exists())
        release_read.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE user_id=:account"), {
                "account": self.account,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": job["id"],
            }).scalar_one(), "cancelled")

    def test_delete_rechecks_jobs_without_reverse_locking_a_requeued_match(self):
        content_key = self.store.write(b"requeue race placeholder")
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:account,'requeue race','2026-10-06','run',false,0,'[]'::json,'[]'::json)
                RETURNING id"""), {"account": self.account}).scalar_one()
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,private_object_key,status)
                VALUES (:account,:activity,'gpx',:key,'succeeded') RETURNING id"""), {
                "account": self.account, "activity": activity_id, "key": content_key,
            }).scalar_one()
            upload_job_id = db.execute(text("""INSERT INTO jobs
                (account_id,kind,dedupe_key,payload)
                VALUES (:account,'process_upload',:dedupe,'{}'::jsonb) RETURNING id"""), {
                "account": self.account, "dedupe": f"upload:{source_id}:r1",
            }).scalar_one()

        first_scan = threading.Event()
        allow_delete = threading.Event()
        source_locked = threading.Event()
        matcher_job_locked = threading.Event()
        matcher_errors = []
        paused = {"initial": False, "source": False}

        def after_execute(connection, cursor, statement, parameters, context, executemany):
            if threading.current_thread().name != "d17-delete":
                return
            compact = " ".join(statement.split()).lower()
            if not paused["initial"] and compact.startswith("select id from jobs") and "for update" in compact:
                paused["initial"] = True
                first_scan.set()
                if not allow_delete.wait(5):
                    raise TimeoutError("delete scan release timed out")
            elif not paused["source"] and compact.startswith("select id,revision,private_object_key from activity_sources"):
                paused["source"] = True
                source_locked.set()
                if not matcher_job_locked.wait(5):
                    raise TimeoutError("matcher never locked the new job")

        event.listen(self.engine, "after_cursor_execute", after_execute)
        delete_errors = []
        deleter = threading.Thread(name="d17-delete", daemon=True, target=lambda: self._capture_error(
            delete_errors, lambda: delete_activity(self.engine, self.account, activity_id)))
        try:
            deleter.start()
            self.assertTrue(first_scan.wait(5), repr(delete_errors))
            with self.engine.begin() as db:
                queue_source_coverage(db, account_id=self.account, source_id=source_id,
                    source_revision=1, activity_id=activity_id)
            with self.engine.connect() as db:
                matcher_job_id = int(db.execute(text("""SELECT job_id FROM coverage_source_runs
                    WHERE account_id=:account AND source_id=:source"""), {
                    "account": self.account, "source": source_id,
                }).scalar_one())

            def matcher_waits_for_source():
                try:
                    with self.engine.begin() as db:
                        db.execute(text("SELECT id FROM jobs WHERE id=:id FOR UPDATE"), {
                            "id": matcher_job_id,
                        }).one()
                        matcher_job_locked.set()
                        db.execute(text("SELECT id FROM activity_sources WHERE id=:id FOR UPDATE"), {
                            "id": source_id,
                        }).first()
                except Exception as exc:
                    matcher_errors.append(exc)

            allow_delete.set()
            self.assertTrue(source_locked.wait(5))
            matcher = threading.Thread(target=matcher_waits_for_source, daemon=True)
            matcher.start()
            self.assertTrue(matcher_job_locked.wait(5))
            deleter.join(5)
            matcher.join(5)
            self.assertFalse(deleter.is_alive())
            self.assertFalse(matcher.is_alive())
            self.assertEqual(delete_errors, [])
            self.assertEqual(matcher_errors, [])
        finally:
            allow_delete.set()
            event.remove(self.engine, "after_cursor_execute", after_execute)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE id=:id"), {
                "id": source_id,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT status FROM jobs WHERE id=:id"), {
                "id": matcher_job_id,
            }).scalar_one(), "cancelled")

    def test_delete_rescans_sources_after_activity_lock_under_read_committed(self):
        object_key = self.store.write(b"late source original")
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:account,'empty source set','2026-10-06','run',false,0,'[]'::json,'[]'::json)
                RETURNING id"""), {"account": self.account}).scalar_one()

        source_scan_done = threading.Event()
        continue_delete = threading.Event()
        paused = False

        def after_execute(connection, cursor, statement, parameters, context, executemany):
            nonlocal paused
            if (threading.current_thread().name == "d17-empty-delete" and not paused
                    and " ".join(statement.split()).lower().startswith(
                        "select id,revision,private_object_key from activity_sources")):
                paused = True
                source_scan_done.set()
                if not continue_delete.wait(5):
                    raise TimeoutError("empty-source delete scan release timed out")

        event.listen(self.engine, "after_cursor_execute", after_execute)
        delete_errors = []
        repeatable_read_engine = self.engine.execution_options(isolation_level="REPEATABLE READ")
        deleter = threading.Thread(name="d17-empty-delete", daemon=True, target=lambda: self._capture_error(
            delete_errors, lambda: delete_activity(repeatable_read_engine, self.account, int(activity_id))))
        try:
            deleter.start()
            self.assertTrue(source_scan_done.wait(5))
            with self.engine.begin() as db:
                source_id = db.execute(text("""INSERT INTO activity_sources
                    (account_id,activity_id,source_kind,private_object_key,status)
                    VALUES (:account,:activity,'gpx',:key,'queued') RETURNING id"""), {
                    "account": self.account, "activity": activity_id, "key": object_key,
                }).scalar_one()
                job_id = db.execute(text("""INSERT INTO jobs
                    (account_id,kind,dedupe_key,payload,priority)
                    VALUES (:account,'process_upload',:dedupe,CAST(:payload AS jsonb),100) RETURNING id"""), {
                    "account": self.account, "dedupe": f"upload:{source_id}:r1",
                    "payload": json.dumps({"source_id": source_id, "revision": 1}),
                }).scalar_one()
            continue_delete.set()
            deleter.join(5)
            self.assertFalse(deleter.is_alive())
            self.assertEqual(delete_errors, [])
        finally:
            continue_delete.set()
            event.remove(self.engine, "after_cursor_execute", after_execute)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM activities WHERE id=:id"), {
                "id": activity_id,
            }).scalar_one(), 0)
            self.assertEqual(db.execute(text("SELECT count(*) FROM activity_sources WHERE id=:id"), {
                "id": source_id,
            }).scalar_one(), 0)
            job = db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}).scalar_one()
            self.assertEqual(job, "cancelled")
            cleanup = dict(db.execute(text("""SELECT * FROM jobs WHERE account_id=:account
                AND kind='delete_private_object'"""), {"account": self.account}).mappings().one())
        self.assertTrue((Path(self.store.root) / object_key).exists())
        process_private_object_cleanup(cleanup, self.store)
        self.assertFalse((Path(self.store.root) / object_key).exists())

    @staticmethod
    def _capture_error(errors, operation):
        try:
            result = operation()
            if hasattr(result, "status_code") and result.status_code != 204:
                errors.append(AssertionError(f"deletion returned {result.status_code}: {result.text}"))
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
