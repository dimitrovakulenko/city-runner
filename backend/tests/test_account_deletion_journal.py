"""Local filesystem guarantees for the independent deletion journal and source owner metadata."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.account_deletion_journal import (
    JournalError,
    append_deletion,
    initialize_ledger,
    read_records,
)
from backend.app import account_deletion_journal
from backend.app.storage import LocalObjectStore


class DeletionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "deletions.jsonl"
        self.env = patch.dict(os.environ, {"ACCOUNT_DELETION_LEDGER": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_init_append_is_idempotent_and_permissions_are_private(self):
        initialize_ledger()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        account = "account-a"
        key = "a" * 32 + ".fit"
        first = append_deletion(account, [key])
        self.assertEqual(append_deletion(account, [key]), first)
        self.assertEqual(read_records(), [first])

    def test_uninitialized_missing_and_corrupt_journal_fail_closed(self):
        with self.assertRaises(JournalError):
            read_records()
        initialize_ledger()
        self.path.write_bytes(b'{"partial":')
        with self.assertRaises(JournalError):
            read_records()

    def test_key_validation_rejects_paths_and_corruption(self):
        initialize_ledger()
        with self.assertRaises(JournalError):
            append_deletion("account-a", ["../private.gpx"])

    def test_new_ledger_directories_are_synced_to_their_parents(self):
        path = Path(self.temp.name) / "one" / "two" / "deletions.jsonl"
        with patch.dict(os.environ, {"ACCOUNT_DELETION_LEDGER": str(path)}):
            synced = []
            original = account_deletion_journal._fsync_directory

            def capture(directory):
                synced.append(Path(directory))
                original(Path(directory))

            with patch.object(account_deletion_journal, "_fsync_directory", side_effect=capture):
                initialize_ledger()
        self.assertIn(Path(self.temp.name), synced)
        self.assertIn(Path(self.temp.name) / "one", synced)
        self.assertIn(Path(self.temp.name) / "one" / "two", synced)

    def test_new_storage_root_directory_is_synced_to_its_parent(self):
        root = Path(self.temp.name) / "one" / "two" / "uploads"
        synced = []
        original = LocalObjectStore._fsync_dir

        def capture(directory):
            synced.append(Path(directory))
            original(Path(directory))

        with patch.object(LocalObjectStore, "_fsync_dir", side_effect=capture):
            LocalObjectStore(root)
        self.assertIn(Path(self.temp.name), synced)
        self.assertIn(Path(self.temp.name) / "one", synced)
        self.assertIn(Path(self.temp.name) / "one" / "two", synced)

    def test_owner_metadata_precedes_bytes_and_is_account_scoped(self):
        root = Path(self.temp.name) / "uploads"
        store = LocalObjectStore(root)
        alice, bob = "account-alice", "account-bob"
        key = store.write(b"synthetic", owner_id=alice)
        directory = store._owner_dir(alice)
        self.assertEqual((directory / f"{key}.owner").read_text(), alice)
        self.assertEqual(store.read(key, owner_id=alice), b"synthetic")
        self.assertIn(key, store.list_owned_keys(alice))
        self.assertNotIn(key, store.list_owned_keys(bob))
        with self.assertRaises(FileNotFoundError):
            store.read(key, owner_id=bob)
        store.delete(key, owner_id=alice)
        self.assertEqual(store.list_owned_keys(alice), [])
        self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
