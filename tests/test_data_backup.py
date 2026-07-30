from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import data_backup


class DataBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.users_path = self.root / "users.json"
        self.tokens_path = self.root / "temp_tokens.json"
        self.accounts_path = self.root / "accounts.json"
        self.users_path.write_text(json.dumps({"users": {"alice": {"id": "u1", "username": "alice"}}}), encoding="utf-8")
        self.tokens_path.write_text(json.dumps({"tokens": {"hash1": {"id": "hash1", "credit_units": 10}}}), encoding="utf-8")
        self.accounts_path.write_text(json.dumps({"accounts": [{"id": "a1", "name": "account", "cookies": []}]}), encoding="utf-8")
        self.patches = [
            patch.object(data_backup.postgres, "enabled", return_value=False),
            patch.object(data_backup, "DATA_DIR", self.root),
            patch.object(data_backup, "USERS_PATH", self.users_path),
            patch.object(data_backup, "ACCOUNTS_PATH", self.accounts_path),
            patch.object(data_backup, "ensure_dirs", return_value=None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_create_backup_contains_required_records(self) -> None:
        raw = data_backup.create_backup()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["format"], data_backup.BACKUP_FORMAT)
            self.assertEqual(metadata["record_counts"], {"users": 1, "temporary_tokens": 1, "accounts": 1})
            self.assertIn("users.json", archive.namelist())
            self.assertIn("temp_tokens.json", archive.namelist())
            self.assertIn("accounts.json", archive.namelist())

    def test_restore_replaces_data_and_keeps_pre_restore_snapshot(self) -> None:
        raw = data_backup.create_backup()
        self.users_path.write_text(json.dumps({"users": {}}), encoding="utf-8")
        self.tokens_path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")
        self.accounts_path.write_text(json.dumps({"accounts": []}), encoding="utf-8")

        result = data_backup.restore_backup(raw)

        self.assertEqual(result["users"], 1)
        self.assertEqual(result["accounts"], 1)
        self.assertIn("alice", json.loads(self.users_path.read_text(encoding="utf-8"))["users"])
        self.assertTrue((self.root / "backups" / result["pre_restore_snapshot"]).is_file())

    def test_restore_rejects_unknown_zip(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("other.json", "{}")
        with self.assertRaisesRegex(ValueError, "missing required files"):
            data_backup.restore_backup(output.getvalue())


if __name__ == "__main__":
    unittest.main()
