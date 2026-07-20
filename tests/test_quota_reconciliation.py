from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import accounts


class QuotaReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.temporary_directory.name) / "accounts.json"
        self.patcher = patch.object(accounts, "ACCOUNTS_PATH", self.accounts_path)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.temporary_directory.cleanup()

    def write_accounts(self, used: int) -> None:
        self.accounts_path.write_text(json.dumps({"accounts": [{"id": "account-1", "name": "账号 1", "quota_used": used}]}), encoding="utf-8")

    def records(self) -> list[dict[str, str]]:
        return [
            {"task_id": "task-1", "account_id": "account-1", "charge_id": "task-1:charge", "finished_at": "2026-07-18T01:00:00+00:00"},
            {"task_id": "task-2", "account_id": "account-1", "charge_id": "task-2:charge", "finished_at": "2026-07-18T02:00:00+00:00"},
        ]

    def test_dry_run_does_not_write(self) -> None:
        self.write_accounts(1)
        before = self.accounts_path.read_text(encoding="utf-8")
        report = accounts.reconcile_success_quota_charges(self.records(), dry_run=True)
        self.assertEqual(report["added_charges"], 2)
        self.assertEqual(report["quota_used_after"], 2)
        self.assertEqual(self.accounts_path.read_text(encoding="utf-8"), before)

    def test_apply_preserves_existing_usage_and_adds_only_missing_usage(self) -> None:
        self.write_accounts(1)
        first = accounts.reconcile_success_quota_charges(self.records(), dry_run=False)
        second = accounts.reconcile_success_quota_charges(self.records(), dry_run=False)
        stored = json.loads(self.accounts_path.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(first["added_charges"], 2)
        self.assertEqual(stored["quota_used"], 2)
        self.assertEqual(len(stored["quota_charges"]), 2)
        self.assertEqual(second["added_charges"], 0)
        self.assertEqual(second["already_present"], 2)

    def test_apply_converts_covered_legacy_usage_without_double_charge(self) -> None:
        self.write_accounts(2)
        report = accounts.reconcile_success_quota_charges(self.records(), dry_run=False)
        stored = json.loads(self.accounts_path.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(report["quota_used_after"], 2)
        self.assertEqual(stored["quota_ledger_base"], 0)


if __name__ == "__main__":
    unittest.main()
