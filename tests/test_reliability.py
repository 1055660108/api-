from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import accounts, store, task_queue, temp_access
from app.worker import WorkerManager, consume_failed_account_quota, refund_account_quota_once, refund_temp_quota_once, should_consume_retry_account_quota


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.accounts_path = self.root / "accounts.json"
        self.tokens_path = self.root / "temp_tokens.json"
        self.patchers = [
            patch.object(store, "TASKS_DIR", self.tasks),
            patch.object(store, "runtime_path", return_value=self.root / "runtime.json"),
            patch.object(accounts, "ACCOUNTS_PATH", self.accounts_path),
            patch.object(temp_access, "TEMP_TOKENS_PATH", self.tokens_path),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def create_task(self, owner: str = "") -> dict:
        return store.create_task("测试任务", "9:16", owner_token_hash=owner)

    def write_account(self, account_id: str = "account1") -> None:
        self.accounts_path.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": account_id,
                            "platform": "dola",
                            "enabled": True,
                            "cookies": [{"name": "session", "value": "value"}],
                            "quota_limit": 2,
                            "quota_used": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_cancelled_worker_cleans_claim_and_account(self) -> None:
        task = self.create_task()
        manager = WorkerManager()

        async def cancel_during_run() -> None:
            worker = asyncio.create_task(manager._worker_loop("worker-1"))
            for _ in range(20):
                if manager._worker_task_ids.get("worker-1") == task["id"]:
                    break
                await asyncio.sleep(0)
            self.assertEqual(manager._worker_task_ids.get("worker-1"), task["id"])
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        asyncio.run(cancel_during_run())
        self.assertNotIn(task["id"], manager._claimed)
        self.assertNotIn("worker-1", manager._worker_task_ids)

    def test_restart_restores_submitted_and_pending_categories(self) -> None:
        submitted = self.create_task()
        pending = self.create_task()
        store.mark_running(submitted["id"], "worker-1")
        store.save_result(submitted["id"], extra={"qianwen_submit_confirmed": True})
        store.mark_running(pending["id"], "worker-2")
        store.reset_running_tasks()
        self.assertEqual(store.get_meta(submitted["id"])["status"], store.STATUS_SUBMITTED)
        self.assertEqual(store.get_meta(pending["id"])["status"], store.STATUS_PENDING)

    def test_expired_video_cleanup_uses_owner_retention_without_touching_active_or_fresh_tasks(self) -> None:
        expired = self.create_task("owner")
        self.assertTrue(store.mark_running(expired["id"], "worker-expired"))
        store.save_result(expired["id"], extra={"decoded_main_url": "https://example.com/expired.mp4"})
        store.mark_success(expired["id"])
        old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        expired_meta = store.get_meta(expired["id"])
        expired_meta.update(finished_at=old_time, updated_at=old_time, created_at=old_time)
        store.meta_path(expired["id"]).write_text(json.dumps(expired_meta), encoding="utf-8")

        fresh = self.create_task("owner")
        self.assertTrue(store.mark_running(fresh["id"], "worker-fresh"))
        store.save_result(fresh["id"], extra={"decoded_main_url": "https://example.com/fresh.mp4"})
        store.mark_success(fresh["id"])
        active = self.create_task("owner")

        result = store.cleanup_expired_task_cache(7, owner_retention_days={"owner": 1})
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(store.task_exists(expired["id"]))
        self.assertTrue(store.task_exists(fresh["id"]))
        self.assertTrue(store.task_exists(active["id"]))
        self.assertIn(active["id"], result["skipped"])

    def test_claim_requires_pending_status_owner_capacity_and_no_cancel(self) -> None:
        canceled = self.create_task("owner")
        store.mark_cancel_requested(canceled["id"])
        limited = self.create_task("limited")
        available = self.create_task("available")
        claimed = store.claim_next_pending(
            "worker-1",
            set(),
            {"limited": 1},
            {"limited": 1},
        )
        self.assertEqual(claimed, available["id"])
        self.assertFalse(store.can_run_task(available["id"], "worker-2"))
        self.assertTrue(store.can_run_task(available["id"], "worker-1"))
        self.assertEqual(store.get_meta(canceled["id"])["status"], store.STATUS_PENDING)
        self.assertEqual(store.get_meta(limited["id"])["status"], store.STATUS_PENDING)

    def test_queued_owner_task_starts_after_submitted_task_finishes(self) -> None:
        submitted = self.create_task("owner")
        queued = self.create_task("owner")
        self.assertTrue(store.mark_running(submitted["id"], "worker-existing"))
        store.mark_submitted(submitted["id"])
        self.assertIsNone(store.claim_next_pending("worker-waiting", set(), {}, {"owner": 1}))
        self.assertEqual(store.get_meta(queued["id"])["status"], store.STATUS_PENDING)
        store.save_result(submitted["id"], extra={"decoded_main_url": "https://example.com/video.mp4"})
        store.mark_success(submitted["id"])
        self.assertEqual(store.claim_next_pending("worker-next", set(), {}, {"owner": 1}), queued["id"])

    def test_submission_barrier_prevents_cancel_refund_window(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        self.assertTrue(store.begin_task_submission(task["id"]))
        canceled, meta = store.request_task_cancel(task["id"])
        self.assertFalse(canceled)
        self.assertEqual(meta["status"], store.STATUS_RUNNING)
        self.assertEqual(meta["submit_phase"], "committing")

    def test_new_attempt_clears_stale_submission_barrier(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        self.assertTrue(store.begin_task_submission(task["id"]))
        store.mark_pending(task["id"], "worker restarted")
        self.assertTrue(store.mark_running(task["id"], "worker-2"))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["submit_phase"], "")
        self.assertEqual(meta["submit_started_at"], "")
        self.assertTrue(store.begin_task_submission(task["id"]))

    def test_cancel_wins_before_submission_barrier(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        canceled, meta = store.request_task_cancel(task["id"])
        self.assertTrue(canceled)
        self.assertEqual(meta["status"], store.STATUS_CANCELED)
        self.assertFalse(store.begin_task_submission(task["id"]))

    def test_file_queue_remains_compatible_with_store_claiming(self) -> None:
        task = self.create_task()
        queue = task_queue.FileTaskQueue()
        claimed = queue.claim("worker-file", set(), {}, {})
        self.assertEqual(claimed, task["id"])
        self.assertTrue(store.can_run_task(task["id"], "worker-file"))
        queue.release(task["id"])

    def test_task_creation_enqueues_through_selected_backend(self) -> None:
        queue = unittest.mock.Mock()
        with patch("app.task_queue.get_task_queue", return_value=queue):
            task = store.create_task("入队任务", "9:16")
        queue.enqueue.assert_called_once_with(task["id"])

    def test_initializing_tasks_are_failed_during_recovery(self) -> None:
        task = store.create_task("未完成创建", "9:16", owner_token_hash="owner", enqueue=False)
        failed = store.fail_initializing_tasks()
        self.assertEqual([item["id"] for item in failed], [task["id"]])
        recovered = store.get_meta(task["id"])
        self.assertEqual(recovered["status"], store.STATUS_FAILED)
        self.assertIn("未成功进入队列", recovered["error"])
        self.assertEqual(store.fail_initializing_tasks(), [])

    def test_redis_release_preserves_delayed_retry_time(self) -> None:
        task = self.create_task()
        available_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        store.update_meta(task["id"], status=store.STATUS_PENDING, next_attempt_at=available_at.isoformat())
        queue = task_queue.RedisTaskQueue.__new__(task_queue.RedisTaskQueue)
        with patch.object(queue, "_delay_claimed") as delay:
            queue.release(task["id"])
        self.assertEqual(delay.call_args.args[0], task["id"])
        self.assertAlmostEqual(delay.call_args.args[1], available_at.timestamp(), delta=0.1)

    def test_submitted_retries_are_forced_back_into_selected_queue(self) -> None:
        for retry_function in (store.retry_submitted_task, store.retry_timed_out_submitted_task):
            with self.subTest(retry_function=retry_function.__name__):
                task = self.create_task()
                store.mark_running(task["id"], "worker-retry")
                store.mark_submitted(task["id"])
                queue = unittest.mock.Mock()
                queue.requeue.return_value = True
                with patch("app.task_queue.get_task_queue", return_value=queue):
                    self.assertEqual(retry_function(task["id"], "结果超时", delay_seconds=10), 1)
                queued_at = queue.requeue.call_args.args[1]
                self.assertEqual(queue.requeue.call_args.args[0], task["id"])
                self.assertIsInstance(queued_at, datetime)
                self.assertGreater(queued_at, datetime.now(timezone.utc))
                self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_PENDING)

    def test_corrupt_task_json_is_not_overwritten(self) -> None:
        task = self.create_task()
        result_path = store.result_path(task["id"])
        result_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(store.CorruptJSONError):
            store.mark_result_once(task["id"], "refunded")
        self.assertEqual(result_path.read_text(encoding="utf-8"), "{broken")

    def test_corrupt_runtime_json_is_reset(self) -> None:
        runtime_path = self.root / "runtime.json"
        runtime_path.write_bytes(b"\x00\x00")
        self.assertEqual(store.load_runtime(), {"active_task_ids": []})
        self.assertEqual(json.loads(runtime_path.read_text(encoding="utf-8")), {"active_task_ids": []})

    def test_refunds_are_idempotent_without_task_result_file(self) -> None:
        task = self.create_task("owner")
        self.write_account()
        self.tokens_path.write_text(
            json.dumps({"tokens": {"owner": {"limit": 10, "used": 1}}}),
            encoding="utf-8",
        )
        refund_account_quota_once(task["id"], "account1")
        store.result_path(task["id"]).unlink()
        refund_account_quota_once(task["id"], "account1")
        refund_temp_quota_once(task["id"], "owner")
        store.result_path(task["id"]).unlink()
        refund_temp_quota_once(task["id"], "owner")
        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 0)
        self.assertEqual(temp_access.get_temp_context("missing"), None)
        token_data = json.loads(self.tokens_path.read_text(encoding="utf-8"))
        self.assertEqual(token_data["tokens"]["owner"]["used"], 0)

    def test_dola_account_defaults_to_one_daily_video(self) -> None:
        created = accounts.add_account("Dola", "session=value")
        self.assertEqual(created["quota_limit"], 1)
        parsed = accounts.parse_bulk_accounts("Dola----session=value")
        self.assertEqual(parsed[0]["quota_limit"], 1)

    def test_bulk_account_import_is_single_write_and_deduplicated(self) -> None:
        raw = "\n".join([f"账号 {index}----session=value-{index}" for index in range(500)] + ["重复账号----session=value-10"])
        with patch.object(accounts, "_write_data", wraps=accounts._write_data) as write_data:
            result = accounts.add_accounts_bulk_result(raw)
        self.assertEqual(result["created"], 500)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(write_data.call_count, 1)
        second = accounts.add_accounts_bulk_result(raw)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped"], 501)
        self.assertEqual(len(accounts.list_accounts()), 500)

    def test_accounts_file_recovers_from_invalid_utf8(self) -> None:
        payload = json.dumps({"accounts": [{"id": "account1", "name": "账号"}]}, ensure_ascii=False).encode("utf-8")
        marker = "账号".encode("utf-8")
        self.accounts_path.write_bytes(payload.replace(marker, marker[:1] + b"\xda" + marker[2:], 1))
        recovered = accounts.list_accounts()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["id"], "account1")
        self.assertTrue(self.accounts_path.with_name("accounts.json.corrupt").exists())
        json.loads(self.accounts_path.read_text(encoding="utf-8"))

    def test_exhaust_account_quota_keeps_account_unavailable(self) -> None:
        created = accounts.add_account("Dola", "session=value")
        self.assertTrue(accounts.exhaust_account_quota(created["id"]))
        exhausted = accounts.list_accounts(platform="dola")[0]
        self.assertEqual(exhausted["quota_limit"], 1)
        self.assertEqual(exhausted["quota_used"], 1)
        self.assertIsNone(accounts.account_for_worker("worker-1", platform="dola"))

    def test_stale_refund_cannot_reduce_a_later_charge(self) -> None:
        created = accounts.add_account("Dola", "session=value", quota_limit=2)
        first = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(first)
        accounts.clear_account_current_task(created["id"], "task-1")
        self.assertTrue(accounts.refund_account_quota(created["id"], first["quota_charge_id"]))
        second = accounts.claim_account_for_worker("worker-2", "task-2")
        self.assertIsNotNone(second)
        accounts.clear_account_current_task(created["id"], "task-2")
        self.assertTrue(accounts.settle_account_quota(created["id"], second["quota_charge_id"]))
        self.assertFalse(accounts.refund_account_quota(created["id"], first["quota_charge_id"]))
        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 1)

    def test_quota_insufficient_refunds_charge_but_exhausts_account(self) -> None:
        created = accounts.add_account("Dola", "session=value", quota_limit=2)
        claimed = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(claimed)
        self.assertTrue(accounts.exhaust_account_quota(created["id"], claimed["quota_charge_id"]))
        exhausted = accounts.list_accounts()[0]
        self.assertEqual(exhausted["quota_used"], 0)
        self.assertIsNone(accounts.account_for_worker("worker-2"))
        self.assertFalse(accounts.refund_account_quota(created["id"], claimed["quota_charge_id"]))

    def test_result_timeout_keeps_charge_and_exhausts_account_for_today(self) -> None:
        created = accounts.add_account("Dola", "session=value", quota_limit=2)
        claimed = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(claimed)
        self.assertTrue(accounts.exhaust_timed_out_account(created["id"], claimed["quota_charge_id"]))
        exhausted = accounts.list_accounts()[0]
        account_data = json.loads(self.accounts_path.read_text(encoding="utf-8"))["accounts"][0]
        charge = next(item for item in account_data["quota_charges"] if item["charge_id"] == claimed["quota_charge_id"])
        self.assertEqual(charge["status"], "settled")
        self.assertEqual(charge["settle_reason"], "result_timeout")
        self.assertEqual(exhausted["quota_used"], 1)
        self.assertEqual(account_data["quota_exhausted_date"], accounts.local_today())
        self.assertIsNone(accounts.account_for_worker("worker-2"))

    def test_dola_retry_keeps_account_quota_consumed(self) -> None:
        accounts.add_account("Dola", "session=value", quota_limit=2)
        claimed = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(claimed)
        consume_failed_account_quota("task-1", claimed, "dola")
        account = accounts.list_accounts()[0]
        self.assertEqual(account["quota_used"], 1)
        data = json.loads(self.accounts_path.read_text(encoding="utf-8"))["accounts"][0]
        charge = next(item for item in data["quota_charges"] if item["charge_id"] == claimed["quota_charge_id"])
        self.assertEqual(charge["status"], "settled")

    def test_infrastructure_retry_does_not_consume_account_quota(self) -> None:
        self.assertFalse(should_consume_retry_account_quota({"retryable": True, "infrastructure_fault": True}))
        self.assertTrue(should_consume_retry_account_quota({"retryable": True, "infrastructure_fault": False}))

    def test_reconciliation_repairs_quota_used_from_charge_ledger(self) -> None:
        created = accounts.add_account("Dola", "session=value", quota_limit=3)
        claimed = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(claimed)
        data = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        data["accounts"][0]["quota_used"] = 0
        self.accounts_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(accounts.reconcile_account_quotas(), {"checked": 1, "repaired": 1})
        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 1)

    def test_result_timeout_requeues_with_another_account(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account1", "account_quota_charge_id": "charge1"})
        store.mark_submitted(task["id"])
        store.update_meta(task["id"], submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat())
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task") as clear_account, patch(
            "app.worker.exhaust_timed_out_account"
        ) as exhaust_account, patch("app.worker.refund_temp_quota_once") as refund_owner, patch(
            "app.worker.query_task", new=AsyncMock()
        ):
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta["result_timeout_retry_count"], 1)
        self.assertIn("account1", meta["failed_account_ids"])
        clear_account.assert_called_once_with("account1", task["id"])
        exhaust_account.assert_called_once_with("account1", "charge1")
        refund_owner.assert_not_called()
        self.assertNotIn("account_id", store.load_result(task["id"]))

    def test_mark_submitted_defers_first_result_poll(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        before = datetime.now(timezone.utc)
        store.mark_submitted(task["id"])
        meta = store.get_meta(task["id"])
        next_poll = datetime.fromisoformat(meta["next_result_poll_at"])
        self.assertEqual(meta["status"], store.STATUS_SUBMITTED)
        self.assertGreaterEqual(next_poll, before + timedelta(seconds=40))
        self.assertLessEqual(next_poll, datetime.now(timezone.utc) + timedelta(seconds=50))

    def test_result_polling_is_parallel_and_respects_concurrency_limit(self) -> None:
        tasks = [self.create_task("owner") for _ in range(8)]
        for index, task in enumerate(tasks):
            store.mark_running(task["id"], f"worker-{index}")
            store.mark_submitted(task["id"])
        manager = WorkerManager()
        active = 0
        peak = 0

        async def query_with_latency(task_id: str) -> dict:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"code": "0", "text": "generating", "url": ""}

        async def exercise() -> None:
            manager._result_poll_semaphore = asyncio.Semaphore(3)
            with patch.object(manager, "_pace_result_poll", new=AsyncMock()), patch(
                "app.worker.query_task", side_effect=query_with_latency
            ):
                await manager._watch_unfinished_success_tasks([task["id"] for task in tasks])

        asyncio.run(exercise())
        self.assertEqual(peak, 3)
        for task in tasks:
            meta = store.get_meta(task["id"])
            self.assertEqual(meta["result_watch_miss_count"], 1)
            self.assertGreater(datetime.fromisoformat(meta["next_result_poll_at"]), datetime.now(timezone.utc))

    def test_one_result_poll_failure_does_not_stop_the_batch(self) -> None:
        manager = WorkerManager()

        async def exercise() -> None:
            with patch.object(
                manager,
                "_watch_unfinished_success_task",
                new=AsyncMock(side_effect=[RuntimeError("database busy"), None, None]),
            ) as watch:
                await manager._watch_unfinished_success_tasks(["a", "b", "c"])
                self.assertEqual(watch.await_count, 3)

        asyncio.run(exercise())
        self.assertEqual(manager._last_error, "database busy")

    def test_unavailable_account_is_requeued_with_backoff(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        manager = WorkerManager()
        self.assertFalse(manager._handle_unavailable_account(task["id"], store.get_meta(task["id"]), "dola"))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertGreater(datetime.fromisoformat(meta["next_attempt_at"]), datetime.now(timezone.utc))

    def test_worker_reuses_token_concurrency_limits_for_one_second(self) -> None:
        manager = WorkerManager()

        async def exercise() -> None:
            with patch("app.worker.temp_token_concurrency_limits", return_value={"owner": 3}) as load_limits:
                self.assertEqual(manager._owner_concurrency_limits(), {"owner": 3})
                self.assertEqual(manager._owner_concurrency_limits(), {"owner": 3})
                load_limits.assert_called_once_with()
                manager._token_concurrency_refreshed_at -= 2
                self.assertEqual(manager._owner_concurrency_limits(), {"owner": 3})
                self.assertEqual(load_limits.call_count, 2)

        asyncio.run(exercise())

    def test_reservation_pruning_keeps_active_and_recent_closed_entries(self) -> None:
        entry = {
            "reservations": {
                **{f"closed-{index}": {"status": "refunded", "created_at": f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"} for index in range(1100)},
                "active-1": {"status": "reserved", "created_at": "2026-01-02T00:00:00+00:00"},
                "active-2": {"status": "reserved", "created_at": "2026-01-02T00:00:01+00:00"},
            }
        }
        temp_access._prune_reservations(entry, max_closed=1000)
        self.assertEqual(len(entry["reservations"]), 1002)
        self.assertIn("active-1", entry["reservations"])
        self.assertIn("active-2", entry["reservations"])

    def test_result_timeout_does_not_requeue_after_cancel_request(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account1", "account_quota_charge_id": "charge1"})
        store.mark_submitted(task["id"])
        store.update_meta(task["id"], submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(), cancel_requested=True)
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task"), patch("app.worker.exhaust_timed_out_account") as exhaust_account, patch(
            "app.worker.refund_temp_quota_once"
        ) as refund_owner:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertNotIn("result_timeout_retry_count", meta)
        exhaust_account.assert_called_once_with("account1", "charge1")
        refund_owner.assert_called_once_with(task["id"], "owner")

    def test_second_result_timeout_stays_pending_for_final_retry(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account2", "account_quota_charge_id": "charge2"})
        store.mark_submitted(task["id"])
        store.update_meta(
            task["id"],
            submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
            result_timeout_retry_count=1,
            failed_account_ids=["account1"],
        )
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task") as clear_account, patch(
            "app.worker.exhaust_timed_out_account"
        ) as exhaust_account, patch("app.worker.refund_temp_quota_once") as refund_owner:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta["result_timeout_retry_count"], 2)
        self.assertEqual(meta["failed_account_ids"], ["account1", "account2"])
        clear_account.assert_called_once_with("account2", task["id"])
        exhaust_account.assert_called_once_with("account2", "charge2")
        refund_owner.assert_not_called()
        self.assertNotIn("account_id", store.load_result(task["id"]))

    def test_third_result_timeout_fails_with_final_reason(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-3")
        store.save_result(task["id"], extra={"account_id": "account3", "account_quota_charge_id": "charge3"})
        store.mark_submitted(task["id"])
        store.update_meta(
            task["id"],
            submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=9)).isoformat(),
            retry_count=2,
            result_timeout_retry_count=2,
            failed_account_ids=["account1", "account2"],
        )
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task"), patch("app.worker.exhaust_timed_out_account"), patch(
            "app.worker.refund_temp_quota_once"
        ) as refund_owner:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertEqual(meta["retry_count"], 3)
        self.assertEqual(meta["result_timeout_retry_count"], 3)
        self.assertEqual(meta["error"], "生成超过8分钟，两次重试后仍未返回结果")
        refund_owner.assert_called_once_with(task["id"], "owner")

    def test_retry_wait_without_available_account_eventually_fails_and_refunds(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-2")
        store.update_meta(
            task["id"],
            result_timeout_retry_count=1,
            retry_queued_at=(datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
        )
        manager = WorkerManager()
        with patch("app.worker.refund_temp_quota_once") as refund_owner:
            self.assertTrue(manager._handle_unavailable_account(task["id"], store.get_meta(task["id"]), "dola"))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertEqual(meta["error"], "重试等待可用账号超时，请重新提交")
        refund_owner.assert_called_once_with(task["id"], "owner")

    def test_listing_globally_timed_out_task_refunds_reserved_owner_quota(self) -> None:
        created = temp_access.create_temp_tokens(1, 1)[0]
        owner_hash = str(created["id"])
        access = temp_access.get_temp_context(str(created["token"]))
        self.assertIsNotNone(access)
        task = self.create_task(owner_hash)
        temp_access.reserve_temp_quota(access, task["id"])
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).free_remaining, 0)
        store.update_meta(task["id"], created_at=(datetime.now(timezone.utc) - timedelta(hours=4)).isoformat())
        listed = next(item for item in store.list_tasks() if item["id"] == task["id"])
        self.assertEqual(listed["status"], store.STATUS_FAILED)
        self.assertEqual(listed["error"], "超时生成失败")
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).free_remaining, 1)
        self.assertTrue(store.load_result(task["id"])["temp_quota_refunded"])

    def test_retry_is_failed_and_refunded_after_thirty_minutes(self) -> None:
        created = temp_access.create_temp_tokens(1, 1)[0]
        owner_hash = str(created["id"])
        access = temp_access.get_temp_context(str(created["token"]))
        task = self.create_task(owner_hash)
        temp_access.reserve_temp_quota(access, task["id"])
        store.update_meta(
            task["id"],
            retry_count=1,
            retry_started_at=(datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),
            status=store.STATUS_PENDING,
        )
        listed = next(item for item in store.list_tasks() if item["id"] == task["id"])
        self.assertEqual(listed["status"], store.STATUS_FAILED)
        self.assertEqual(listed["error"], "重试超过30分钟，生成失败")
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).free_remaining, 1)

    def test_retry_budget_is_shared_across_execution_and_result_timeout(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        self.assertEqual(store.record_retry(task["id"], "首次失败"), 1)
        self.assertTrue(store.mark_running(task["id"], "worker-2"))
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_timed_out_submitted_task(task["id"], "结果超时"), 2)
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta["retry_count"], 2)
        self.assertEqual(meta["result_timeout_retry_count"], 1)
        self.assertTrue(store.mark_running(task["id"], "worker-3"))
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_timed_out_submitted_task(task["id"], "结果超时"), 3)
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_FAILED)

    def test_legacy_retry_override_cannot_exceed_global_limit(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_submitted_task(task["id"], "额度不足", max_retries=5), 1)
        self.assertTrue(store.mark_running(task["id"], "worker-2"))
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_submitted_task(task["id"], "额度不足", max_retries=5), 2)
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_PENDING)
        self.assertTrue(store.mark_running(task["id"], "worker-3"))
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_submitted_task(task["id"], "额度不足", max_retries=5), 3)
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_FAILED)

    def test_watchdog_scan_error_does_not_stop_watchdog(self) -> None:
        manager = WorkerManager()
        manager._stopping = False

        async def run_watchdog() -> None:
            calls = 0

            async def scan() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("scan failed")
                manager._stopping = True

            async def no_wait(_seconds: float) -> None:
                return None

            with patch.object(manager, "_watch_running_tasks_once", new=scan), patch("app.worker.asyncio.sleep", new=no_wait):
                await manager._watch_running_tasks()
            self.assertEqual(calls, 2)
            self.assertEqual(manager._restart_count, 1)
            self.assertEqual(manager._last_error, "scan failed")

        asyncio.run(run_watchdog())

    def test_start_is_idempotent(self) -> None:
        manager = WorkerManager()

        async def start_twice() -> None:
            with patch("app.worker.reset_running_tasks") as reset, patch("app.worker.queue_backend", return_value="file"):
                await manager.start()
                supervisor = manager._supervisor
                watchdog = manager._watchdog
                await manager.start()
                self.assertIs(manager._supervisor, supervisor)
                self.assertIs(manager._watchdog, watchdog)
                reset.assert_called_once()
                await manager.stop()

        asyncio.run(start_twice())

    def test_deduct_points_is_atomic_and_preserves_free_quota(self) -> None:
        self.tokens_path.write_text(
            json.dumps({"tokens": {"owner": {"limit": 11, "used": 1}}}),
            encoding="utf-8",
        )
        result = temp_access.deduct_temp_points("owner", 1, 3)
        self.assertEqual(result["credit_units"], 70)
        with self.assertRaisesRegex(ValueError, "积分不足"):
            temp_access.deduct_temp_points("owner", 1, 8)
        token_data = json.loads(self.tokens_path.read_text(encoding="utf-8"))
        self.assertEqual(token_data["tokens"]["owner"]["credit_units"], 70)


if __name__ == "__main__":
    unittest.main()
