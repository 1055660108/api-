from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app import accounts, package_catalog, postgres, store, temp_access, users
from scripts import storage_migrate


class MemoryPostgres:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, dict]] = {}
        self.documents: dict[str, dict] = {}
        self.lock = threading.RLock()

    def ensure_schema(self) -> None:
        return None

    def task_exists(self, task_id: str) -> bool:
        return task_id in self.tasks

    def create_task(self, task_id: str, meta: dict) -> bool:
        if task_id in self.tasks:
            return False
        self.tasks[task_id] = {"meta": dict(meta), "result": {}}
        return True

    def find_or_create_idempotent_task(self, task_id: str, meta: dict) -> tuple[dict, bool]:
        with self.lock:
            for task in self.tasks.values():
                existing = task["meta"]
                if (
                    existing.get("owner_token_hash") == meta.get("owner_token_hash")
                    and existing.get("request_route") == meta.get("request_route")
                    and existing.get("idempotency_hash") == meta.get("idempotency_hash")
                ):
                    if existing.get("request_fingerprint") != meta.get("request_fingerprint"):
                        raise ValueError("idempotency key conflicts with a different request")
                    return dict(existing), False
            self.tasks[task_id] = {"meta": dict(meta), "result": {}}
            return dict(meta), True

    def read_task_part(self, task_id: str, part: str, default=None) -> dict:
        if task_id not in self.tasks:
            if part == "meta" and default is None:
                raise FileNotFoundError(task_id)
            return {} if default is None else dict(default)
        return dict(self.tasks[task_id][part])

    def mutate_task_part(self, task_id: str, part: str, mutator):
        with self.lock:
            if task_id not in self.tasks:
                raise FileNotFoundError(task_id)
            payload = deepcopy(self.tasks[task_id][part])
            result = mutator(payload)
            self.tasks[task_id][part] = payload
            return result

    def write_task_part(self, task_id: str, part: str, payload: dict) -> None:
        if task_id not in self.tasks:
            raise FileNotFoundError(task_id)
        self.tasks[task_id][part] = dict(payload)

    def claim_task(self, task_id: str, worker_id: str, owner_token_hash: str, concurrency_limit: int | None, claimed_at: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            meta = task["meta"]
            if meta.get("status") != "pending" or meta.get("cancel_requested"):
                return False
            if owner_token_hash and concurrency_limit is not None:
                active = sum(
                    item["meta"].get("owner_token_hash") == owner_token_hash
                    and item["meta"].get("status") in {"running", "submitted"}
                    for item in self.tasks.values()
                )
                if active >= concurrency_limit:
                    return False
            meta.update(
                status="running",
                worker_id=worker_id,
                started_at=claimed_at,
                claimed_at=claimed_at,
                attempt=max(0, int(meta.get("attempt") or 0)) + 1,
                error="",
                execution_miss_count=0,
                updated_at=claimed_at,
            )
            return True

    def list_task_ids(self) -> list[str]:
        return list(self.tasks)

    def list_task_metas(self, owner_token_hash: str | None = None) -> list[tuple[str, dict]]:
        return [
            (task_id, dict(task["meta"]))
            for task_id, task in self.tasks.items()
            if owner_token_hash is None or task["meta"].get("owner_token_hash") == owner_token_hash
        ]

    def count_tasks(self, status: str | None = None) -> int:
        return sum(status is None or task["meta"].get("status") == status for task in self.tasks.values())

    def delete_task(self, task_id: str) -> None:
        self.tasks.pop(task_id, None)

    def read_document(self, name: str, default=None) -> dict:
        if name not in self.documents:
            return {} if default is None else dict(default)
        return dict(self.documents[name])

    def write_document(self, name: str, payload: dict) -> None:
        self.documents[name] = dict(payload)

    def mutate_document(self, name: str, default: dict, mutator):
        with self.lock:
            payload = deepcopy(self.documents.get(name, default))
            result = mutator(payload)
            self.documents[name] = payload
            return result

    def clear_all(self) -> None:
        self.tasks.clear()
        self.documents.clear()


class PostgresStorageCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.backend = MemoryPostgres()
        self.patchers = [
            patch.object(postgres, "enabled", return_value=True),
            patch.object(postgres, "ensure_schema", self.backend.ensure_schema),
            patch.object(postgres, "task_exists", self.backend.task_exists),
            patch.object(postgres, "create_task", self.backend.create_task),
            patch.object(postgres, "find_or_create_idempotent_task", self.backend.find_or_create_idempotent_task),
            patch.object(postgres, "read_task_part", self.backend.read_task_part),
            patch.object(postgres, "write_task_part", self.backend.write_task_part),
            patch.object(postgres, "mutate_task_part", self.backend.mutate_task_part),
            patch.object(postgres, "claim_task", self.backend.claim_task),
            patch.object(postgres, "list_task_ids", self.backend.list_task_ids),
            patch.object(postgres, "list_task_metas", self.backend.list_task_metas),
            patch.object(postgres, "count_tasks", self.backend.count_tasks),
            patch.object(postgres, "delete_task", self.backend.delete_task),
            patch.object(postgres, "read_document", self.backend.read_document),
            patch.object(postgres, "write_document", self.backend.write_document),
            patch.object(postgres, "mutate_document", self.backend.mutate_document),
            patch.object(postgres, "clear_all", self.backend.clear_all),
            patch.object(store, "TASKS_DIR", self.root / "tasks"),
            patch.object(store, "runtime_path", return_value=self.root / "runtime.json"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_task_public_functions_keep_existing_semantics(self) -> None:
        task = store.create_task("兼容测试", "9:16", owner_token_hash="owner")
        task_id = task["id"]
        self.assertTrue(store.task_exists(task_id))
        self.assertTrue(store.mark_running(task_id, "worker-1"))
        store.save_result(task_id, conversation_id="conversation", extra={"decoded_main_url": "https://video"})
        store.mark_success(task_id)
        self.assertEqual(store.get_meta(task_id)["status"], store.STATUS_SUCCESS)
        self.assertEqual(store.load_result(task_id)["conversation_id"], "conversation")
        self.assertEqual(store.list_tasks(owner_token_hash="owner")[0]["id"], task_id)
        store.delete_task(task_id)
        self.assertFalse(store.task_exists(task_id))

    def test_document_modules_use_postgres_without_signature_changes(self) -> None:
        self.backend.documents["accounts"] = {"accounts": [{"id": "account1", "platform": "dola", "enabled": True}]}
        self.backend.documents["temp_tokens"] = {"tokens": {}}
        self.backend.documents["users"] = {"users": {}}
        self.backend.documents["point_packages"] = {"packages": []}
        self.assertEqual(accounts.list_accounts()[0]["id"], "account1")
        self.assertEqual(temp_access.list_temp_tokens(), [])
        self.assertEqual(users.list_users([]), [])
        self.assertEqual(package_catalog.list_packages(), [])

    def test_task_status_claim_is_atomic_across_process_facades(self) -> None:
        task = store.create_task("并发领取", "9:16")
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda index: store.mark_running(task["id"], f"worker-{index}"), range(20)))
        self.assertEqual(claimed.count(True), 1)
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_RUNNING)

    def test_idempotent_task_creation_is_atomic_across_process_facades(self) -> None:
        def create(_: int) -> tuple[dict, bool]:
            return store.find_or_create_task(
                "并发幂等创建",
                "9:16",
                "owner",
                "dola",
                "Seedance 2.0",
                "video",
                "same-key",
                "same-fingerprint",
                "tasks",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create, range(20)))
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len({meta["id"] for meta, _ in results}), 1)
        self.assertEqual(len(self.backend.tasks), 1)

    def test_account_claim_and_refund_are_atomic_across_process_facades(self) -> None:
        self.backend.documents["accounts"] = {
            "accounts": [{"id": "account1", "platform": "dola", "enabled": True, "cookies": [{"name": "session", "value": "value"}], "quota_limit": 1, "quota_used": 0}]
        }
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(lambda index: accounts.claim_account_for_worker(f"worker-{index}", f"task-{index}"), range(20)))
        successful = [claim for claim in claims if claim]
        self.assertEqual(len(successful), 1)
        charge_id = successful[0]["quota_charge_id"]
        with ThreadPoolExecutor(max_workers=8) as executor:
            refunds = list(executor.map(lambda _: accounts.refund_account_quota("account1", charge_id), range(20)))
        self.assertEqual(refunds.count(True), 1)
        self.assertEqual(self.backend.documents["accounts"]["accounts"][0]["quota_used"], 0)

    def test_user_quota_reserve_and_refund_are_atomic_across_process_facades(self) -> None:
        token_hash = "owner"
        self.backend.documents["temp_tokens"] = {"tokens": {token_hash: {"billing_version": 2, "free_remaining": 1, "credit_units": 0, "reservations": {}}}}
        access = temp_access.get_temp_context_from_entry(token_hash, self.backend.documents["temp_tokens"]["tokens"][token_hash])

        def reserve(index: int) -> bool:
            try:
                temp_access.reserve_temp_quota(access, f"task-{index}", 10)
                return True
            except temp_access.QuotaExceeded:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            reservations = list(executor.map(reserve, range(20)))
        self.assertEqual(reservations.count(True), 1)
        reserved_id = next(iter(self.backend.documents["temp_tokens"]["tokens"][token_hash]["reservations"]))
        with ThreadPoolExecutor(max_workers=8) as executor:
            refunds = list(executor.map(lambda _: temp_access.refund_temp_quota_hash(token_hash, reserved_id), range(20)))
        self.assertEqual(refunds.count(True), 1)
        self.assertEqual(self.backend.documents["temp_tokens"]["tokens"][token_hash]["free_remaining"], 1)

    def test_json_migration_and_rollback_round_trip(self) -> None:
        task_id = "a" * 32
        task_dir = self.root / "tasks" / task_id
        (task_dir / "images").mkdir(parents=True)
        (task_dir / "images" / "1.png").write_bytes(b"image")
        (task_dir / "meta.json").write_text(json.dumps({"id": task_id, "status": "pending"}), encoding="utf-8")
        (task_dir / "result.json").write_text(json.dumps({"conversation_id": "c"}), encoding="utf-8")
        (self.root / "accounts.json").write_text(json.dumps({"accounts": [{"id": "account1"}]}), encoding="utf-8")
        backup = self.root.with_name(f"{self.root.name}-backup")
        result = storage_migrate.migrate_to_postgres(self.root, backup)
        self.assertEqual(result, {"tasks": 1, "documents": 1})
        self.assertEqual(self.backend.tasks[task_id]["result"]["conversation_id"], "c")
        shutil_target = self.root / "tasks"
        if shutil_target.exists():
            import shutil
            shutil.rmtree(shutil_target)
        (self.root / "accounts.json").unlink()
        rollback = storage_migrate.rollback_to_json(self.root, backup)
        self.assertEqual(rollback, {"tasks": 1, "documents": 1})
        self.assertEqual(json.loads((self.root / "tasks" / task_id / "meta.json").read_text())["id"], task_id)
        self.assertEqual((self.root / "tasks" / task_id / "images" / "1.png").read_bytes(), b"image")

    def test_migration_rejects_api_or_worker_running_markers(self) -> None:
        for marker_name in (".service-running", ".worker-health.json"):
            marker = self.root / marker_name
            marker.write_text("running", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                storage_migrate._assert_stopped(marker, False)
            storage_migrate._assert_stopped(marker, True)
            marker.unlink()


if __name__ == "__main__":
    unittest.main()
