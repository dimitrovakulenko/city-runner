import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from xml.etree.ElementTree import Element, SubElement, tostring

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.app.coverage import (get_account_coverage, get_street_coverage, queue_source_coverage,
                                  rebuild_account_coverage, remove_source_support, requeue_dataset_coverage)
from backend.app import coverage as coverage_module
from backend.app.geography import import_osm_xml
from backend.app.jobs import claim
from backend.app.coverage import process_source_dataset
from backend.app.main import create_app
from backend.app.storage import LocalObjectStore
from backend.app.uploads import process_upload
from backend.app.worker import run_once


DATABASE_URL = os.getenv("POSTGIS_TEST_DATABASE_URL")
DATABASE_PATTERN = __import__("re").compile(r"city_runner_test_[0-9a-f]{12}\Z")


def fixture_xml() -> bytes:
    root = Element("osm", {"version": "0.6", "generator": "coverage-test"})
    nodes = {
        1: (-0.01, -0.01), 2: (0.01, -0.01), 3: (0.01, 0.01), 4: (-0.01, 0.01),
    }
    main_ids = list(range(10001, 10011))
    for i, node_id in enumerate(main_ids):
        nodes[node_id] = (i * 0.0007, 0)
    parallel_ids = [10101, 10102, 10103]
    for i, node_id in enumerate(parallel_ids):
        nodes[node_id] = (i * 0.0007, 0.0003)
    gap_ids = [10201, 10202, 10203]
    for node_id, lon in zip(gap_ids, (0.004, 0.0045, 0.005)):
        nodes[node_id] = (lon, -0.004)
    tiny_ids = [10301, 10302, 10303]
    for i, node_id in enumerate(tiny_ids):
        nodes[node_id] = (0.001 + i * 0.0007, 0.004)
    boundary_ids = [10401, 10402]
    nodes[10401] = (0.008, 0.006)
    nodes[10402] = (0.009, 0.006)
    for node_id, (lon, lat) in nodes.items():
        SubElement(root, "node", {"id": str(node_id), "lon": str(lon), "lat": str(lat)})

    ways = [
        (100, [1, 2, 3, 4, 1], {}),
        (500, main_ids, {"highway": "residential", "name": "Main Street"}),
        (501, parallel_ids, {"highway": "residential", "name": "Parallel Road"}),
        (502, gap_ids, {"highway": "residential", "name": "Gap Street"}),
        (503, tiny_ids, {"highway": "residential", "name": "Tiny Street"}),
        (504, boundary_ids, {"highway": "residential", "name": "Boundary Road"}),
    ]
    for way_id, refs, tags in ways:
        way = SubElement(root, "way", {"id": str(way_id)})
        for ref in refs:
            SubElement(way, "nd", {"ref": str(ref)})
        for key, value in tags.items():
            SubElement(way, "tag", {"k": key, "v": value})
    relation = SubElement(root, "relation", {"id": "900"})
    SubElement(relation, "member", {"type": "way", "ref": "100", "role": "outer"})
    for key, value in {"type": "boundary", "boundary": "administrative", "admin_level": "8",
                       "name": "Coverage Fixture"}.items():
        SubElement(relation, "tag", {"k": key, "v": value})
    return tostring(root, encoding="utf-8", xml_declaration=True)


@unittest.skipUnless(DATABASE_URL, "run with the disposable PostGIS integration harness")
class CoveragePostgisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATABASE_PATTERN.fullmatch(make_url(DATABASE_URL).database or ""):
            raise RuntimeError("coverage integration tests require a generated disposable database")
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
        self.region = f"coverage-{uuid.uuid4().hex}"
        self.accounts = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.tmp = tempfile.TemporaryDirectory()
        self.osm_path = Path(self.tmp.name) / "coverage.osm"
        self.osm_path.write_bytes(fixture_xml())
        dataset = import_osm_xml(self.engine, self.osm_path, region=self.region,
                                 city_relation_ids=[900], source_timestamp="2026-10-01T00:00:00Z",
                                 coverage_mode="complete", coverage_evidence="Synthetic complete fixture")
        self.dataset_id = dataset["dataset_id"]
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO accounts(id) VALUES (:a),(:b)"), {
                "a": self.accounts[0], "b": self.accounts[1],
            })

    def tearDown(self):
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activities WHERE user_id=ANY(:ids)"), {"ids": self.accounts})
            db.execute(text("DELETE FROM accounts WHERE id=ANY(:ids)"), {"ids": self.accounts})
            db.execute(text("DELETE FROM map_datasets WHERE id=:id"), {"id": self.dataset_id})
        self.tmp.cleanup()

    def add_source(self, account_index: int, tracks: list[list[list[float]]]) -> tuple[int, int]:
        account_id = self.accounts[account_index]
        with self.engine.begin() as db:
            activity_id = db.execute(text("""INSERT INTO activities
                (user_id,name,date,activity_type,processed,unmapped_points,tracks,timestamps)
                VALUES (:account_id,'Synthetic coverage','2026-10-05','running',false,0,
                    CAST(:tracks AS json), CAST(:timestamps AS json)) RETURNING id"""), {
                "account_id": account_id, "tracks": json.dumps(tracks),
                "timestamps": json.dumps([[None for _ in segment] for segment in tracks]),
            }).scalar_one()
            source_id = db.execute(text("""INSERT INTO activity_sources
                (account_id,activity_id,source_kind,revision,status)
                VALUES (:account_id,:activity_id,'gpx',1,'succeeded') RETURNING id"""), {
                "account_id": account_id, "activity_id": activity_id,
            }).scalar_one()
            queued = queue_source_coverage(db, account_id=account_id, source_id=source_id,
                                           source_revision=1, activity_id=activity_id)
        return int(source_id), int(activity_id)

    def run_match(self, account_id: str, source_id: int, revision: int = 1):
        with self.engine.connect() as db:
            job_id = db.execute(text("""SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account_id AND source_id=:source_id
                  AND source_revision=:revision AND dataset_id=:dataset_id"""), {
                "account_id": account_id, "source_id": source_id,
                "revision": revision, "dataset_id": self.dataset_id,
            }).scalar_one()
        job = claim(self.engine, kind="match_coverage")
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], job_id)
        process_source_dataset(self.engine, job)

    def street_id(self, name: str) -> int:
        with self.engine.connect() as db:
            return int(db.execute(text("""SELECT id FROM streets
                WHERE dataset_id=:dataset AND display_name=:name"""), {
                "dataset": self.dataset_id, "name": name,
            }).scalar_one())

    def test_25m_parallel_roads_gaps_thresholds_and_overlap_removal(self):
        alice = self.accounts[0]
        with self.engine.connect() as db:
            boundary_samples = db.execute(text("""SELECT ST_X(ST_Project(point,:distance,0)::geometry),
                    ST_Y(ST_Project(point,:distance,0)::geometry)
                FROM osm_nodes WHERE dataset_id=:dataset AND osm_node_id=10401"""), {"dataset": self.dataset_id,
                                            "distance": 24.99}).one()
            outside_sample = db.execute(text("""SELECT ST_X(ST_Project(point,25.01,0)::geometry),
                    ST_Y(ST_Project(point,25.01,0)::geometry)
                FROM osm_nodes WHERE dataset_id=:dataset AND osm_node_id=10401"""), {
                "dataset": self.dataset_id,
            }).one()
        first = [
            [[i * 0.0007, 0] for i in range(8)],
            [[0.001, 0.004], [0.0017, 0.004]],
            [[float(boundary_samples[0]), float(boundary_samples[1])],
             [float(outside_sample[0]), float(outside_sample[1])]],
            [[0.004, -0.004], [0.005, -0.004]],
        ]
        source_a, _ = self.add_source(0, first)
        self.run_match(alice, source_a)
        main = self.street_id("Main Street")
        tiny = self.street_id("Tiny Street")
        parallel = self.street_id("Parallel Road")
        gap = self.street_id("Gap Street")
        boundary = self.street_id("Boundary Road")
        with self.engine.connect() as db:
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main), {
                "status": "ready", "dataset_id": str(self.dataset_id), "street_id": str(main),
                "rule": "normal", "visited": 8, "total": 10, "threshold": 9, "state": "partial",
            })
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main, "strict")["state"], "partial")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, tiny)["state"], "partial")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, parallel)["visited"], 0)
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, gap)["visited"], 2)
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, boundary)["visited"], 1)
            gap_center = db.execute(text("""SELECT osm_node_id FROM osm_nodes
                WHERE dataset_id=:dataset AND osm_node_id=10202"""), {"dataset": self.dataset_id}).scalar_one()
            self.assertEqual(db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND dataset_id=:dataset AND node_id=:node"""), {
                "account": alice, "dataset": self.dataset_id, "node": gap_center,
            }).scalar_one(), 0)

        second, _ = self.add_source(0, [[[0.0056, 0], [0, 0], [0.0024, 0.004]]])
        self.run_match(alice, second)
        with self.engine.connect() as db:
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main)["state"], "complete")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main, "strict")["state"], "partial")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, tiny)["state"], "complete")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, tiny, "strict")["state"], "complete")
            self.assertEqual(db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": alice, "source": second, "dataset": self.dataset_id,
            }).scalar_one(), 3)
            # Duplicate queueing is idempotent and does not create a second job or support row.
            activity_id = db.execute(text("SELECT activity_id FROM activity_sources WHERE id=:id"), {
                "id": source_a,
            }).scalar_one()
            before = db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:account AND kind='match_coverage'"), {
                "account": alice,
            }).scalar_one()
        with self.engine.begin() as db:
            queue_source_coverage(db, account_id=alice, source_id=source_a, source_revision=1,
                                  activity_id=activity_id)
        with self.engine.connect() as db:
            after = db.execute(text("SELECT count(*) FROM jobs WHERE account_id=:account AND kind='match_coverage'"), {
                "account": alice,
            }).scalar_one()
        self.assertEqual(before, after)

        with self.engine.begin() as db:
            removed = remove_source_support(db, account_id=alice, source_id=source_a, source_revision=1)
        self.assertEqual(removed, [self.dataset_id])
        with self.engine.connect() as db:
            # Node 10001 is shared by both activities, so it survives removal of source A.
            shared = db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND dataset_id=:dataset AND node_id=10001"""), {
                "account": alice, "dataset": self.dataset_id,
            }).scalar_one()
            self.assertEqual(shared, 1)
            remaining = db.execute(text("""SELECT count(DISTINCT node_id) FROM source_node_contributions
                WHERE account_id=:account AND dataset_id=:dataset AND source_id=:source"""), {
                "account": alice, "dataset": self.dataset_id, "source": second,
            }).scalar_one()
            self.assertEqual(remaining, 3)

    def test_account_isolation_stale_lease_revision_and_missing_geography(self):
        alice, bob = self.accounts
        source_a, _ = self.add_source(0, [[[0, 0]]])
        with self.engine.connect() as db:
            job_id = db.execute(text("""SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source"""), {
                "account": alice, "source": source_a,
            }).scalar_one()
        stale_job = claim(self.engine, kind="match_coverage")
        with self.engine.begin() as db:
            db.execute(text("UPDATE jobs SET leased_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {
                "id": job_id,
            })
        current_job = claim(self.engine, kind="match_coverage")
        self.assertNotEqual(stale_job["lease_token"], current_job["lease_token"])
        process_source_dataset(self.engine, stale_job)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM source_node_contributions WHERE source_id=:id"), {
                "id": source_a,
            }).scalar_one(), 0)
        process_source_dataset(self.engine, current_job)
        with self.engine.connect() as db:
            bob_empty = get_account_coverage(db, bob, self.dataset_id)
            self.assertEqual(bob_empty["status"], "ready")
            self.assertEqual(bob_empty["visited_node_count"], 0)
        source_b, _ = self.add_source(1, [[[0, 0]]])
        self.run_match(bob, source_b)
        main = self.street_id("Main Street")
        with self.engine.connect() as db:
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main)["visited"], 1)
            self.assertEqual(get_street_coverage(db, bob, self.dataset_id, main)["visited"], 1)
            self.assertEqual(get_account_coverage(db, alice, self.dataset_id)["status"], "ready")
            self.assertEqual(get_account_coverage(db, bob, self.dataset_id)["status"], "ready")
            self.assertEqual(get_account_coverage(db, alice, 999_999)["status"], "pending")

        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=revision WHERE id=:id"), {"id": source_a})
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM source_node_contributions WHERE source_id=:id"), {
                "id": source_a,
            }).scalar_one(), 1)

        with self.engine.begin() as db:
            db.execute(text("UPDATE activity_sources SET revision=2 WHERE id=:id"), {"id": source_a})
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM source_node_contributions WHERE source_id=:id"), {
                "id": source_a,
            }).scalar_one(), 0)
            self.assertEqual(get_account_coverage(db, alice, self.dataset_id)["status"], "pending")
            self.assertIsNone(get_account_coverage(db, alice, self.dataset_id)["visited_node_count"])
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main)["visited"], None)
            activity_id = db.execute(text("SELECT activity_id FROM activity_sources WHERE id=:id"), {
                "id": source_a,
            }).scalar_one()
            self.assertFalse(db.execute(text("SELECT processed FROM activities WHERE id=:id"), {
                "id": activity_id,
            }).scalar_one())

    def test_bounded_requeue_and_unsupported_samples_are_explicit(self):
        alice = self.accounts[0]
        sources = [self.add_source(0, [[[0.5, 0.5]]])[0] for _ in range(2)]
        # Remove the jobs created by normal ingestion to model legacy succeeded sources.
        with self.engine.begin() as db:
            db.execute(text("""DELETE FROM coverage_source_runs WHERE account_id=:account
                AND source_id=ANY(:sources)"""), {"account": alice, "sources": sources})
            db.execute(text("""DELETE FROM jobs WHERE account_id=:account AND kind='match_coverage'"""), {
                "account": alice,
            })
        page = requeue_dataset_coverage(self.engine, self.dataset_id, batch_size=1, max_sources=1)
        self.assertEqual(page["sources_queued"], 1)
        self.assertTrue(page["has_more"])
        next_page = requeue_dataset_coverage(self.engine, self.dataset_id, batch_size=1, max_sources=1,
                                             after_source_id=page["next_after_source_id"])
        self.assertEqual(next_page["sources_queued"], 1)
        self.assertFalse(next_page["has_more"])
        job = claim(self.engine, kind="match_coverage")
        process_source_dataset(self.engine, job)
        job = claim(self.engine, kind="match_coverage")
        process_source_dataset(self.engine, job)
        with self.engine.connect() as db:
            summary = get_account_coverage(db, alice, self.dataset_id)
            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["visited_node_count"], 0)
            self.assertEqual(summary["unsupported_sample_count"], 2)
            for source_id in sources:
                activity_id = db.execute(text("SELECT activity_id FROM activity_sources WHERE id=:id"), {
                    "id": source_id,
                }).scalar_one()
                self.assertEqual(db.execute(text("SELECT unmapped_points FROM activities WHERE id=:id"), {
                    "id": activity_id,
                }).scalar_one(), 1)

    def test_direct_source_delete_invalidates_cache_but_preserves_overlap(self):
        alice = self.accounts[0]
        first, _ = self.add_source(0, [[[0, 0]]])
        second, _ = self.add_source(0, [[[0, 0]]])
        self.run_match(alice, first)
        self.run_match(alice, second)
        main = self.street_id("Main Street")
        with self.engine.begin() as db:
            db.execute(text("DELETE FROM activity_sources WHERE id=:id AND account_id=:account"), {
                "id": first, "account": alice,
            })
        with self.engine.connect() as db:
            self.assertEqual(get_account_coverage(db, alice, self.dataset_id)["status"], "ready")
            self.assertEqual(get_street_coverage(db, alice, self.dataset_id, main)["visited"], 1)
            self.assertEqual(db.execute(text("""SELECT count(*) FROM account_street_coverage
                WHERE account_id=:account AND dataset_id=:dataset"""), {
                "account": alice, "dataset": self.dataset_id,
            }).scalar_one(), 0)
        rebuild_account_coverage(self.engine, alice, self.dataset_id)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("""SELECT visited_node_count FROM account_street_coverage
                WHERE account_id=:account AND dataset_id=:dataset AND street_id=:street"""), {
                "account": alice, "dataset": self.dataset_id, "street": main,
            }).scalar_one(), 1)

    def test_requeue_during_matching_has_consistent_lock_order_and_keeps_ready_state(self):
        alice = self.accounts[0]
        source_id, activity_id = self.add_source(0, [[[0, 0]]])
        with self.engine.connect() as db:
            job_id = db.execute(text("""SELECT job_id FROM coverage_source_runs
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": alice, "source": source_id, "dataset": self.dataset_id,
            }).scalar_one()
        job = claim(self.engine, kind="match_coverage")
        self.assertEqual(job["id"], job_id)
        inside_batch, release_batch = threading.Event(), threading.Event()
        errors = []
        original_insert = coverage_module._insert_batch

        def pause_after_batch(*args, **kwargs):
            original_insert(*args, **kwargs)
            inside_batch.set()
            if not release_batch.wait(5):
                raise RuntimeError("coverage concurrency test timed out")

        def run_match():
            try:
                process_source_dataset(self.engine, job)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(coverage_module, "_insert_batch", side_effect=pause_after_batch):
            matcher = threading.Thread(target=run_match)
            matcher.start()
            self.assertTrue(inside_batch.wait(5))
            requeue_result = {}

            def run_requeue():
                try:
                    requeue_result.update(requeue_dataset_coverage(
                        self.engine, self.dataset_id, batch_size=1, max_sources=1,
                        after_source_id=source_id - 1,
                    ))
                except BaseException as exc:
                    errors.append(exc)

            requeue = threading.Thread(target=run_requeue)
            requeue.start()
            time.sleep(0.05)
            release_batch.set()
            matcher.join(5)
            requeue.join(5)
        self.assertFalse(matcher.is_alive())
        self.assertFalse(requeue.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(requeue_result["sources_queued"], 1)
        with self.engine.connect() as db:
            self.assertEqual(get_account_coverage(db, alice, self.dataset_id)["status"], "ready")
            self.assertTrue(db.execute(text("SELECT processed FROM activities WHERE id=:id"), {
                "id": activity_id,
            }).scalar_one())
            self.assertEqual(db.execute(text("""SELECT count(*) FROM source_node_contributions
                WHERE account_id=:account AND source_id=:source AND dataset_id=:dataset"""), {
                "account": alice, "source": source_id, "dataset": self.dataset_id,
            }).scalar_one(), 1)

    def test_exhausted_coverage_job_is_failed_and_hides_counts(self):
        alice = self.accounts[0]
        source_id, _ = self.add_source(0, [[[0, 0]]])
        with self.engine.begin() as db:
            db.execute(text("""UPDATE activities SET tracks='null'::json
                WHERE user_id=:account AND id=(SELECT activity_id FROM activity_sources WHERE id=:source)"""), {
                "account": alice, "source": source_id,
            })
        for attempt in range(1, 6):
            job = claim(self.engine, kind="match_coverage")
            self.assertIsNotNone(job)
            self.assertEqual(job["attempts"], attempt)
            with self.assertRaisesRegex(ValueError, "invalid_activity_tracks"):
                process_source_dataset(self.engine, job)
            if attempt < 5:
                with self.engine.begin() as db:
                    db.execute(text("UPDATE jobs SET available_at=clock_timestamp() WHERE id=:id"), {
                        "id": job["id"],
                    })
        with self.engine.connect() as db:
            row = db.execute(text("""SELECT job.status AS job_status, run.status AS run_status,
                    run.last_error FROM jobs job JOIN coverage_source_runs run
                      ON run.account_id=job.account_id AND run.job_id=job.id
                WHERE run.account_id=:account AND run.source_id=:source"""), {
                "account": alice, "source": source_id,
            }).mappings().one()
            summary = get_account_coverage(db, alice, self.dataset_id)
        self.assertEqual(row["job_status"], "failed")
        self.assertEqual(row["run_status"], "failed")
        self.assertEqual(row["last_error"], "coverage_invalid_activity_tracks")
        self.assertEqual(summary["status"], "failed")
        self.assertIsNone(summary["visited_node_count"])

    def test_upload_transaction_enqueues_coverage_and_worker_finishes_activity(self):
        alice = self.accounts[0]
        root = Path(self.tmp.name) / "private-uploads"
        store = LocalObjectStore(root)
        xml = b'''<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
          <trk><name>Coverage import</name><trkseg>
            <trkpt lat="0" lon="0"><time>2026-10-05T08:00:00Z</time></trkpt>
          </trkseg></trk></gpx>'''
        client = TestClient(create_app(self.engine, lambda: alice))
        try:
            with patch.dict(os.environ, {"UPLOAD_STORAGE_DIR": str(root)}):
                response = client.post("/api/uploads", files={"file": ("run.gpx", xml, "application/gpx+xml")})
                self.assertEqual(response.status_code, 202, response.text)
                upload = response.json()
                self.assertTrue(run_once(self.engine, {
                    "process_upload": lambda job: process_upload(self.engine, job, store),
                }))
                with self.engine.connect() as db:
                    run = db.execute(text("""SELECT run.job_id,run.status FROM coverage_source_runs run
                        WHERE run.account_id=:account AND run.source_id=:source AND run.dataset_id=:dataset"""), {
                        "account": alice, "source": int(upload["id"]), "dataset": self.dataset_id,
                    }).mappings().one()
                self.assertEqual(run["status"], "queued")
                self.assertTrue(run_once(self.engine, {
                    "match_coverage": lambda job: process_source_dataset(self.engine, job),
                }))
                status = client.get(f"/api/uploads/{upload['id']}").json()
        finally:
            client.close()
        self.assertEqual(status["status"], "succeeded")
        with self.engine.connect() as db:
            activity = db.execute(text("SELECT processed,unmapped_points FROM activities WHERE id=:id"), {
                "id": int(status["activity_id"]),
            }).one()
            self.assertEqual(tuple(activity), (True, 0))


if __name__ == "__main__":
    unittest.main()
