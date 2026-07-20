from __future__ import annotations

import os
import unittest
from concurrent.futures import ProcessPoolExecutor
from uuid import uuid4

from app import postgres


def claim_task(args: tuple[str, str]) -> bool:
    from app import store

    return store.mark_running(*args)


def add_result_field(args: tuple[str, int]) -> None:
    from app import store

    task_id, index = args
    store.save_result(task_id, extra={f"field_{index}": index})


def claim_account(args: tuple[str, str]) -> dict | None:
    from app import accounts

    return accounts.claim_account_for_worker(*args)


def refund_account(args: tuple[str, str]) -> bool:
    from app import accounts

    return accounts.refund_account_quota(*args)


def reserve_quota(args: tuple[str, str]) -> bool:
    from app import temp_access

    token_hash, task_id = args
    entry = postgres.read_document("temp_tokens")["tokens"][token_hash]
    access = temp_access.get_temp_context_from_entry(token_hash, entry)
    try:
        temp_access.reserve_temp_quota(access, task_id, 10)
    except temp_access.QuotaExceeded:
        return False
    return True


def refund_quota(args: tuple[str, str]) -> bool:
    from app import temp_access

    return temp_access.refund_temp_quota_hash(*args)


@unittest.skipUnless(os.environ.get("DOLA_TEST_DATABASE_URL"), "DOLA_TEST_DATABASE_URL is not configured")
class PostgresProcessConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_database_url = os.environ.get("DOLA_DATABASE_URL")
        os.environ["DOLA_DATABASE_URL"] = os.environ["DOLA_TEST_DATABASE_URL"]
        postgres.ensure_schema()

    @classmethod
    def tearDownClass(cls) -> None:
        postgres.clear_all()
        if cls.original_database_url is None:
            os.environ.pop("DOLA_DATABASE_URL", None)
        else:
            os.environ["DOLA_DATABASE_URL"] = cls.original_database_url

    def setUp(self) -> None:
        postgres.clear_all()

    def test_task_claim_and_result_merges_are_atomic_across_processes(self) -> None:
        task_id = uuid4().hex
        self.assertTrue(postgres.create_task(task_id, {"id": task_id, "status": "pending", "created_at": "2026-01-01T00:00:00+00:00"}))
        with ProcessPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(claim_task, [(task_id, f"worker-{index}") for index in range(24)]))
        self.assertEqual(claims.count(True), 1)
        with ProcessPoolExecutor(max_workers=8) as executor:
            list(executor.map(add_result_field, [(task_id, index) for index in range(24)]))
        result = postgres.read_task_part(task_id, "result")
        self.assertEqual({result.get(f"field_{index}") for index in range(24)}, set(range(24)))

    def test_account_claim_and_refund_are_atomic_across_processes(self) -> None:
        postgres.write_document("accounts", {"accounts": [{"id": "account1", "platform": "dola", "enabled": True, "cookies": [{"name": "session", "value": "value"}], "quota_limit": 1, "quota_used": 0}]})
        with ProcessPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(claim_account, [(f"worker-{index}", f"task-{index}") for index in range(24)]))
        successful = [claim for claim in claims if claim]
        self.assertEqual(len(successful), 1)
        charge_id = successful[0]["quota_charge_id"]
        with ProcessPoolExecutor(max_workers=8) as executor:
            refunds = list(executor.map(refund_account, [("account1", charge_id)] * 24))
        self.assertEqual(refunds.count(True), 1)
        self.assertEqual(postgres.read_document("accounts")["accounts"][0]["quota_used"], 0)

    def test_user_reserve_and_refund_are_atomic_across_processes(self) -> None:
        token_hash = "owner"
        postgres.write_document("temp_tokens", {"tokens": {token_hash: {"billing_version": 2, "free_remaining": 1, "credit_units": 0, "reservations": {}}}})
        with ProcessPoolExecutor(max_workers=8) as executor:
            reservations = list(executor.map(reserve_quota, [(token_hash, f"task-{index}") for index in range(24)]))
        self.assertEqual(reservations.count(True), 1)
        entry = postgres.read_document("temp_tokens")["tokens"][token_hash]
        reserved_id = next(iter(entry["reservations"]))
        with ProcessPoolExecutor(max_workers=8) as executor:
            refunds = list(executor.map(refund_quota, [(token_hash, reserved_id)] * 24))
        self.assertEqual(refunds.count(True), 1)
        entry = postgres.read_document("temp_tokens")["tokens"][token_hash]
        self.assertEqual(entry["free_remaining"], 1)


if __name__ == "__main__":
    unittest.main()
