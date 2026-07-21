from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, package_catalog, point_transactions, temp_access, users
from app.billing import model_cost_units, package_bonus_free_uses, points_to_units, units_to_points


class BillingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tokens_path = self.root / "temp_tokens.json"
        self.users_path = self.root / "users.json"
        self.packages_path = self.root / "point_packages.json"
        self.patchers = [
            patch.object(config, "CONFIG_PATH", self.root / "config.json"),
            patch.object(config, "DATA_DIR", self.root),
            patch.object(config, "TASKS_DIR", self.root / "tasks"),
            patch.object(temp_access, "TEMP_TOKENS_PATH", self.tokens_path),
            patch.object(users, "USERS_PATH", self.users_path),
            patch.object(package_catalog, "PACKAGE_CATALOG_PATH", self.packages_path),
            patch.object(point_transactions, "TRANSACTIONS_PATH", self.root / "point_transactions.json"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_point_units_are_exact_to_one_decimal(self) -> None:
        self.assertEqual(points_to_units("0.1"), 1)
        self.assertEqual(points_to_units(12.3), 123)
        self.assertEqual(units_to_points(123), 12.3)
        self.assertEqual(units_to_points(120), 12)
        with self.assertRaisesRegex(ValueError, "0.1"):
            points_to_units("0.01")

    def test_model_costs_use_integer_units(self) -> None:
        self.assertEqual(model_cost_units("dola", "Seedance 2.0"), 10)
        self.assertEqual(model_cost_units("qianwen", "万相 2.7"), 8)
        self.assertEqual(model_cost_units("qianwen", "万相 2.6"), 5)
        self.assertEqual(model_cost_units("qianwen", "HappyHorse 1.0"), 8)

    def test_model_costs_can_be_configured_by_platform(self) -> None:
        config.ensure_config()
        config.update_config({"model_costs": {"dola": {"Seedance 2.0": 1.7}, "doubao": {}, "qianwen": {}}})
        self.assertEqual(model_cost_units("dola", "Seedance 2.0"), 17)
        self.assertEqual(model_cost_units("qianwen", "万相 2.7"), 8)

    def test_free_quota_is_reserved_before_credits(self) -> None:
        token = temp_access.create_temp_tokens(1, 1)[0]
        temp_access.add_temp_credit_units(token["id"], 20)
        access = temp_access.get_temp_context(token["token"])
        first = temp_access.reserve_temp_quota(access, "task-free", 8)
        self.assertEqual(first.free_remaining, 0)
        self.assertEqual(first.credit_units, 20)
        second = temp_access.reserve_temp_quota(first, "task-paid", 8)
        self.assertEqual(second.credit_units, 12)

    def test_points_priority_uses_points_then_falls_back_to_video_quota(self) -> None:
        token = temp_access.create_temp_tokens(1, 2)[0]
        temp_access.add_temp_credit_units(token["id"], 10)
        temp_access.set_temp_billing_priority(token["id"], "points_first")
        access = temp_access.get_temp_context(token["token"])
        self.assertEqual(access.billing_priority, "points_first")
        paid = temp_access.reserve_temp_quota(access, "task-paid", 8)
        self.assertEqual(paid.credit_units, 2)
        self.assertEqual(paid.free_remaining, 2)
        free = temp_access.reserve_temp_quota(paid, "task-free", 8)
        self.assertEqual(free.credit_units, 2)
        self.assertEqual(free.free_remaining, 1)

    def test_task_refund_is_idempotent_for_free_and_paid_reservations(self) -> None:
        token = temp_access.create_temp_tokens(1, 1)[0]
        temp_access.add_temp_credit_units(token["id"], 20)
        access = temp_access.get_temp_context(token["token"])
        paid_access = temp_access.reserve_temp_quota(access, "task-free", 8)
        temp_access.reserve_temp_quota(paid_access, "task-paid", 8)
        self.assertTrue(temp_access.refund_temp_quota_hash(token["id"], "task-free"))
        self.assertFalse(temp_access.refund_temp_quota_hash(token["id"], "task-free"))
        self.assertTrue(temp_access.refund_temp_quota_hash(token["id"], "task-paid"))
        self.assertFalse(temp_access.refund_temp_quota_hash(token["id"], "task-paid"))
        data = json.loads(self.tokens_path.read_text(encoding="utf-8"))["tokens"][token["id"]]
        self.assertEqual(data["free_remaining"], 1)
        self.assertEqual(data["credit_units"], 20)

    def test_paid_refund_records_one_ledger_entry(self) -> None:
        token = temp_access.create_temp_tokens(1, 1)[0]
        temp_access.add_temp_credit_units(token["id"], 20)
        access = temp_access.get_temp_context(token["token"])
        paid_access = temp_access.reserve_temp_quota(access, "task-free", 8, user_id="user-1")
        temp_access.reserve_temp_quota(paid_access, "task-paid", 8, user_id="user-1")
        self.assertTrue(temp_access.refund_temp_quota_hash(token["id"], "task-paid"))
        self.assertFalse(temp_access.refund_temp_quota_hash(token["id"], "task-paid"))
        rows = point_transactions.list_transactions("user-1")["transactions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "refund")
        self.assertEqual(rows[0]["amount"], 0.8)

    def test_free_quota_refund_records_video_quota_and_task_id(self) -> None:
        token = temp_access.create_temp_tokens(1, 1)[0]
        access = temp_access.get_temp_context(token["token"])
        temp_access.reserve_temp_quota(access, "task-free", 8, user_id="user-1")
        self.assertTrue(temp_access.refund_temp_quota_hash(token["id"], "task-free"))
        self.assertFalse(temp_access.refund_temp_quota_hash(token["id"], "task-free"))
        rows = point_transactions.list_transactions("user-1")["transactions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "video_quota_refund")
        self.assertEqual(rows[0]["amount"], 0)
        self.assertEqual(rows[0]["video_quota_change"], 1)
        self.assertEqual(rows[0]["video_quota_balance"], 1)
        self.assertEqual(rows[0]["reference_id"], "task-free")
        self.assertIn("任务 ID：task-free", rows[0]["detail"])

    def test_old_user_migration_preserves_free_and_paid_balance(self) -> None:
        self.tokens_path.write_text(json.dumps({"tokens": {"owner": {"limit": 8, "used": 2}}}), encoding="utf-8")
        self.assertTrue(temp_access.migrate_temp_token("owner", 3))
        self.assertFalse(temp_access.migrate_temp_token("owner", 3))
        data = json.loads(self.tokens_path.read_text(encoding="utf-8"))["tokens"]["owner"]
        self.assertEqual(data["free_remaining"], 1)
        self.assertEqual(data["credit_units"], 50)

    def test_package_bonus_free_uses_follow_fixed_tiers(self) -> None:
        self.assertEqual(package_bonus_free_uses(18), 0)
        self.assertEqual(package_bonus_free_uses(30), 6)
        self.assertEqual(package_bonus_free_uses(68), 14)
        self.assertEqual(package_bonus_free_uses(128), 26)
        self.assertEqual(package_bonus_free_uses(256), 51)
        self.assertEqual([item["points"] for item in package_catalog.list_packages()], [1, 6, 18, 30, 68, 128, 256])

    def test_package_catalog_supports_publish_adjust_and_disable(self) -> None:
        created = package_catalog.create_package({"name": "测试套餐", "points": 12.5, "bonus_free_uses": 3, "sort_order": 0, "payment_url": "https://pay.example.com/package-a"})
        self.assertEqual(created["points"], 12.5)
        self.assertEqual(created["payment_url"], "https://pay.example.com/package-a")
        adjusted = package_catalog.update_package(created["id"], {"points": 15, "name": "调整套餐", "payment_url": "https://pay.example.com/package-b"})
        self.assertEqual(adjusted["name"], "调整套餐")
        self.assertEqual(adjusted["points"], 15)
        self.assertEqual(adjusted["payment_url"], "https://pay.example.com/package-b")
        package_catalog.disable_package(created["id"])
        self.assertNotIn(created["id"], [item["id"] for item in package_catalog.list_packages()])
        self.assertIn(created["id"], [item["id"] for item in package_catalog.list_packages(include_disabled=True)])

    def test_user_credit_does_not_mix_bonus_free_uses(self) -> None:
        registered = users.register_user("billing_user", "password123")
        user_id = users.list_users(temp_access.list_temp_tokens())[0]["id"]
        result = users.add_user_points(user_id, 50, temp_access.list_temp_tokens())
        self.assertEqual(result, {"purchased": 50, "credited": 50})
        balance = users.user_balance_by_token_hash(temp_access.hash_token(registered["token"]), temp_access.list_temp_tokens())
        self.assertEqual(balance["free_remaining"], 1)
        self.assertEqual(balance["points"], 50)


if __name__ == "__main__":
    unittest.main()
