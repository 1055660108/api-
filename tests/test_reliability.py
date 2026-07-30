from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import accounts, automation, config, store, task_queue, temp_access
from app.automation import DolaFetchAutomation, is_infrastructure_failure
from app.resilience import fair_owner_capacity_limits
from app.worker import IMAGE_PREPARATION_CONCURRENCY, IMAGE_SUBMISSION_CONCURRENCY, WorkerManager, consume_failed_account_quota, defer_non_counting_retry, refund_account_quota_once, refund_temp_quota_once, release_account_after_retryable_failure, should_consume_retry_account_quota


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
            patch.object(config, "CONFIG_PATH", self.root / "config.json"),
            patch.object(config, "DATA_DIR", self.root),
            patch.object(config, "TASKS_DIR", self.tasks),
            patch.dict("os.environ", {"DOLA_ADMIN_PASSWORD": "ReliabilityTestPassword123"}),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def create_task(self, owner: str = "") -> dict:
        return store.create_task("测试任务", "9:16", owner_token_hash=owner)

    def test_postgres_active_task_count_uses_database_aggregate(self) -> None:
        with patch.object(store.postgres, "enabled", return_value=True), patch.object(
            store.postgres, "count_active_tasks_for_owner", return_value=37
        ) as count:
            self.assertEqual(store.active_task_count_for_owner("owner-fast-count"), 37)
        count.assert_called_once_with("owner-fast-count")

    def test_dola_submission_pacing_is_global_across_public_exits(self) -> None:
        manager = WorkerManager()

        async def exercise() -> list[float]:
            manager._last_dola_submit_at = asyncio.get_running_loop().time()
            waits: list[float] = []

            async def record_sleep(delay: float) -> None:
                waits.append(delay)

            with patch("app.worker.load_settings", return_value=SimpleNamespace(dola_global_submit_interval_seconds=5.0)), patch(
                "app.worker.asyncio.sleep", new=AsyncMock(side_effect=record_sleep)
            ):
                await asyncio.gather(
                    manager._wait_for_dola_submit_slot("ip:203.0.113.10"),
                    manager._wait_for_dola_submit_slot("ip:203.0.113.11"),
                )
            return waits

        waits = asyncio.run(exercise())
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(wait > 4.9 for wait in waits))

    def test_global_submit_interval_migrates_existing_exit_interval(self) -> None:
        config.CONFIG_PATH.write_text(
            json.dumps({"dola_exit_submit_interval_seconds": 19.0}),
            encoding="utf-8",
        )

        settings = config.load_settings()

        self.assertEqual(settings.dola_global_submit_interval_seconds, 19.0)
        persisted = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(persisted["dola_global_submit_interval_seconds"], 19.0)

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
        ambiguous = self.create_task()
        pending = self.create_task()
        store.mark_running(submitted["id"], "worker-1")
        store.save_result(submitted["id"], extra={"qianwen_submit_confirmed": True})
        store.mark_running(ambiguous["id"], "worker-ambiguous")
        store.save_result(ambiguous["id"], extra={"submission_ambiguous": True, "submit_confirmation_state": "awaiting_conversation"})
        store.mark_running(pending["id"], "worker-2")
        store.reset_running_tasks()
        self.assertEqual(store.get_meta(submitted["id"])["status"], store.STATUS_SUBMITTED)
        self.assertEqual(store.get_meta(ambiguous["id"])["status"], store.STATUS_SUBMITTED)
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

    def test_calendar_disk_cleanup_keeps_yesterday_and_active_tasks(self) -> None:
        old_finished = self.create_task()
        self.assertTrue(store.mark_running(old_finished["id"], "worker-old"))
        store.save_result(old_finished["id"], extra={"decoded_main_url": "https://example.com/old.mp4"})
        store.mark_success(old_finished["id"])

        yesterday = self.create_task()
        self.assertTrue(store.mark_running(yesterday["id"], "worker-yesterday"))
        store.save_result(yesterday["id"], extra={"decoded_main_url": "https://example.com/yesterday.mp4"})
        store.mark_success(yesterday["id"])

        old_active = self.create_task()
        self.assertTrue(store.mark_running(old_active["id"], "worker-active"))

        def set_created_at(task_id: str, created_at: str) -> None:
            meta = store.get_meta(task_id)
            meta["created_at"] = created_at
            store.meta_path(task_id).write_text(json.dumps(meta), encoding="utf-8")

        set_created_at(old_finished["id"], "2026-07-28T23:59:59+08:00")
        set_created_at(yesterday["id"], "2026-07-29T00:00:00+08:00")
        set_created_at(old_active["id"], "2026-07-28T12:00:00+08:00")

        result = store.cleanup_terminal_tasks_before_local_day(
            now=datetime(2026, 7, 30, 12, tzinfo=store.LOCAL_TZ),
        )

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["cutoff_local"], "2026-07-29T00:00:00+08:00")
        self.assertEqual(result["skipped"]["active"], 1)
        self.assertFalse(store.task_exists(old_finished["id"]))
        self.assertTrue(store.task_exists(yesterday["id"]))
        self.assertTrue(store.task_exists(old_active["id"]))

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

    def test_submitted_task_does_not_hold_browser_submission_concurrency(self) -> None:
        submitted = self.create_task("owner")
        queued = self.create_task("owner")
        self.assertTrue(store.mark_running(submitted["id"], "worker-existing"))
        store.mark_submitted(submitted["id"])
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

    def test_running_task_exposes_execution_phase(self) -> None:
        task = self.create_task()
        self.assertTrue(store.mark_running(task["id"], "worker-phase"))
        listed = next(item for item in store.list_tasks() if item["id"] == task["id"])
        self.assertEqual(listed["execution_phase"], "waiting_account")
        self.assertEqual(listed["status_reason"], "正在分配生成资源")
        self.assertTrue(listed["phase_updated_at"])

        self.assertTrue(store.set_execution_phase(task["id"], "opening_generation_page", "正在打开生成页面"))
        updated = store.get_meta(task["id"])
        self.assertEqual(updated["execution_phase"], "opening_generation_page")
        self.assertEqual(updated["status_reason"], "正在打开生成页面")

    def test_watchdog_recovers_orphaned_running_task(self) -> None:
        task = self.create_task()
        self.assertTrue(store.mark_running(task["id"], "worker-missing"))
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        store.update_meta(task["id"], started_at=stale, phase_updated_at=stale)
        manager = WorkerManager()
        manager._queue = unittest.mock.Mock()

        async def recover() -> None:
            await manager._watch_running_tasks_once()

        asyncio.run(recover())
        recovered = store.get_meta(task["id"])
        self.assertEqual(recovered["status"], store.STATUS_PENDING)
        self.assertEqual(recovered["infrastructure_retry_count"], 1)
        manager._queue.requeue.assert_called_once_with(task["id"])

    def test_watchdog_cancels_stale_live_execution(self) -> None:
        task = self.create_task()
        self.assertTrue(store.mark_running(task["id"], "worker-stale"))
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        store.update_meta(task["id"], started_at=stale, phase_updated_at=stale, execution_phase="opening_generation_page")
        manager = WorkerManager()

        async def recover() -> None:
            live = asyncio.create_task(asyncio.sleep(60))
            manager._workers["worker-stale"] = live
            await manager._watch_running_tasks_once()
            with self.assertRaises(asyncio.CancelledError):
                await live

        asyncio.run(recover())
        recovered = store.get_meta(task["id"])
        self.assertEqual(recovered["status"], store.STATUS_PENDING)
        self.assertEqual(recovered["infrastructure_retry_count"], 1)
        self.assertIn("opening_generation_page", recovered["infrastructure_error"])

    def test_watchdog_requeues_due_pending_retry(self) -> None:
        task = self.create_task()
        self.assertTrue(store.mark_running(task["id"], "worker-retry-reconcile"))
        store.mark_submitted(task["id"])
        self.assertEqual(store.retry_submitted_task(task["id"], "远端生成失败", delay_seconds=1), 1)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.update_meta(task["id"], next_attempt_at=due, retry_queue_verified_at="", status_reason="正在重试中，请稍等！")
        manager = WorkerManager()
        manager._queue = unittest.mock.Mock()
        manager._queue.requeue.return_value = True

        async def reconcile() -> None:
            await manager._watch_running_tasks_once()

        asyncio.run(reconcile())
        recovered = store.get_meta(task["id"])
        self.assertEqual(recovered["status"], store.STATUS_PENDING)
        self.assertEqual(recovered["status_reason"], "重试已入队，等待执行")
        self.assertTrue(recovered["retry_queue_verified_at"])
        manager._queue.requeue.assert_called_with(task["id"], datetime.fromisoformat(due))

    def test_startup_requeues_tasks_left_by_legacy_dola_guard(self) -> None:
        task = self.create_task()
        store.defer_task(task["id"], "平台服务繁忙，任务已自动排队", "platform_guard", 3600)
        manager = WorkerManager()
        manager._queue = unittest.mock.Mock()
        manager._queue.requeue.return_value = True

        manager._requeue_stale_dola_guard_tasks()

        recovered = store.get_meta(task["id"])
        self.assertEqual(recovered["status"], store.STATUS_PENDING)
        self.assertEqual(recovered["queue_category"], "")
        self.assertEqual(recovered["queue_reason"], "等待重新提交")
        self.assertLessEqual(datetime.fromisoformat(recovered["next_attempt_at"]), datetime.now(timezone.utc))
        manager._queue.requeue.assert_called_once_with(task["id"])

    def test_startup_requeues_tasks_blocked_by_old_image_and_browser_limits(self) -> None:
        image_wait = self.create_task("owner-image-wait")
        browser_wait = self.create_task("owner-browser-wait")
        unrelated = self.create_task("owner-unrelated")
        store.defer_task(image_wait["id"], "等待参考图上传资源", "image_upload_limit", 3600)
        store.update_meta(
            browser_wait["id"],
            infrastructure_retry_count=5,
            infrastructure_error="execution phase timed out: allocating_browser",
            queue_category="infrastructure",
            queue_reason="服务连接异常，正在恢复",
            next_attempt_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        manager = WorkerManager()
        manager._queue = unittest.mock.Mock()
        manager._queue.requeue.return_value = True

        manager._requeue_stale_resource_wait_tasks()

        recovered_image = store.get_meta(image_wait["id"])
        recovered_browser = store.get_meta(browser_wait["id"])
        untouched = store.get_meta(unrelated["id"])
        self.assertEqual(recovered_image["queue_category"], "")
        self.assertEqual(recovered_image["status_reason"], "系统资源已恢复，等待重新提交")
        self.assertEqual(recovered_browser["infrastructure_retry_count"], 0)
        self.assertEqual(recovered_browser["infrastructure_error"], "")
        self.assertEqual(recovered_browser["queue_category"], "")
        self.assertNotEqual(untouched.get("status_reason"), "系统资源已恢复，等待重新提交")
        self.assertEqual(
            {call.args[0] for call in manager._queue.requeue.call_args_list},
            {image_wait["id"], browser_wait["id"]},
        )

    def test_task_creation_enqueues_through_selected_backend(self) -> None:
        queue = unittest.mock.Mock()
        with patch("app.task_queue.get_task_queue", return_value=queue):
            task = store.create_task("入队任务", "9:16")
        queue.enqueue.assert_called_once_with(task["id"])

    def test_finalized_bulk_retry_is_enqueued_at_its_scheduled_release_time(self) -> None:
        task = store.create_task("间隔放行任务", "9:16", enqueue=False)
        available_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        store.update_meta(task["id"], next_attempt_at=available_at.isoformat())
        queue = unittest.mock.Mock()
        with patch("app.task_queue.get_task_queue", return_value=queue):
            store.finalize_task_creation(task["id"])
        queued_at = queue.enqueue.call_args.args[1]
        self.assertEqual(queue.enqueue.call_args.args[0], task["id"])
        self.assertAlmostEqual(queued_at.timestamp(), available_at.timestamp(), delta=0.1)

    def test_due_redis_retries_are_promoted_ahead_of_newer_ready_tasks(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "task_queue.py").read_text(encoding="utf-8")
        self.assertIn("for index = #tasks, 1, -1 do", source)
        self.assertIn("redis.call('RPUSH', KEYS[2], task_id)", source)

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

    def test_worker_can_claim_the_preferred_account_for_proxy_retry(self) -> None:
        accounts.add_account("Dola A", "session=first", quota_limit=2)
        preferred = accounts.add_account("Dola B", "session=second", quota_limit=2)
        claimed = accounts.claim_account_for_worker(
            "worker-1",
            "task-1",
            platform="dola",
            preferred_id=preferred["id"],
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], preferred["id"])

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
        self.assertTrue(is_infrastructure_failure("all eligible proxy nodes are temporarily unavailable"))

    def test_infrastructure_failure_refunds_claimed_account_quota(self) -> None:
        account = accounts.add_account("Dola", "session=value", quota_limit=2)
        task = self.create_task("owner")
        claimed = accounts.claim_account_for_worker("worker-1", task["id"])
        self.assertIsNotNone(claimed)
        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 1)

        release_account_after_retryable_failure(
            task["id"],
            claimed,
            "dola",
            {"retryable": True, "infrastructure_fault": True},
        )

        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 0)
        self.assertIsNone(accounts.account_for_current_task(task["id"]))

    def test_login_invalid_account_is_abnormal_until_cookies_are_replaced(self) -> None:
        created = accounts.add_account("Expired", "session=expired", quota_limit=2)
        disabled = accounts.disable_account_for_login(created["id"], "Dola 登录状态失效（游客模式）")

        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["account_status"], "abnormal")
        self.assertIn("游客模式", disabled["status_reason"])
        self.assertIsNone(accounts.account_for_worker("worker-1"))
        accounts.set_account_enabled(created["id"], True)
        self.assertIsNone(accounts.account_for_worker("worker-1"))

        restored = accounts.update_account_cookies(created["id"], accounts.parse_cookie_payload("session=fresh"))
        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["account_status"], "normal")
        self.assertEqual(restored["status_reason"], "")
        self.assertIsNotNone(accounts.account_for_worker("worker-1"))

    def test_ten_second_dola_account_stays_normal_but_is_not_scheduled(self) -> None:
        created = accounts.add_account("Ten seconds", "session=ten-seconds", quota_limit=2)

        marked = accounts.mark_account_ten_second_limit(created["id"])

        self.assertTrue(marked["ten_second_only"])
        self.assertTrue(marked["ten_second_marked_at"])
        self.assertTrue(marked["enabled"])
        self.assertEqual(marked["account_status"], "normal")
        self.assertIsNone(accounts.account_for_worker("worker-ten-seconds"))
        cleaned = accounts.cleanup_flagged_accounts(datetime(2030, 1, 1, 23, 0, tzinfo=accounts.LOCAL_TZ))
        self.assertEqual(cleaned["removed"], 0)
        restored = accounts.update_account_cookies(created["id"], accounts.parse_cookie_payload("session=ten-seconds-restored"))
        self.assertFalse(restored["ten_second_only"])
        self.assertEqual(accounts.account_for_worker("worker-ten-seconds-restored")["id"], created["id"])

    def test_slider_and_abnormal_accounts_are_deleted_at_23_with_daily_statistics(self) -> None:
        task = self.create_task("slider-owner")
        created = accounts.add_account("Slider", "session=slider", quota_limit=2)
        claimed = accounts.claim_account_for_worker("worker-slider", task["id"])
        self.assertEqual(claimed["id"], created["id"])

        with patch.object(accounts, "local_today", return_value="2030-01-01"), patch.object(
            accounts, "utc_now", return_value="2030-01-01T12:00:00+08:00"
        ):
            release_account_after_retryable_failure(
                task["id"],
                claimed,
                "dola",
                {
                    "retryable": True,
                    "account_fault": True,
                    "account_slider_verification": True,
                },
            )

        stored = accounts.list_accounts()[0]
        self.assertTrue(stored["enabled"])
        self.assertEqual(stored["account_status"], "slider_verification")
        self.assertIn("跳验证", stored["status_reason"])
        self.assertEqual(stored["slider_verification_streak"], 1)
        self.assertEqual(stored["quota_used"], 0)
        self.assertIsNone(accounts.account_for_worker("worker-next"))

        abnormal = accounts.add_account("Abnormal", "session=abnormal", quota_limit=2)
        with patch.object(accounts, "utc_now", return_value="2030-01-01T13:00:00+08:00"):
            accounts.disable_account_for_login(abnormal["id"], "登录失效")
        manually_disabled = accounts.add_account("Manual", "session=manual", enabled=False, quota_limit=2)

        before = accounts.cleanup_flagged_accounts(datetime(2030, 1, 1, 22, 59, tzinfo=accounts.LOCAL_TZ))
        self.assertEqual(before["removed"], 0)
        self.assertEqual(len(accounts.list_accounts()), 3)

        cleaned = accounts.cleanup_flagged_accounts(datetime(2030, 1, 1, 23, 0, tzinfo=accounts.LOCAL_TZ))
        self.assertEqual(cleaned["removed"], 2)
        self.assertEqual(cleaned["by_status"], {"abnormal": 1, "slider_verification": 1})
        self.assertEqual([item["id"] for item in accounts.list_accounts()], [manually_disabled["id"]])
        history = accounts.list_account_deletion_history()
        self.assertEqual(history[0]["date"], "2030-01-01")
        self.assertEqual(history[0]["total"], 2)
        self.assertEqual(history[0]["by_status"], {"abnormal": 1, "slider_verification": 1})

        repeated = accounts.cleanup_flagged_accounts(datetime(2030, 1, 1, 23, 1, tzinfo=accounts.LOCAL_TZ))
        self.assertEqual(repeated["removed"], 0)
        self.assertEqual(accounts.list_account_deletion_history()[0]["total"], 2)

    def test_service_frequent_account_state_detects_slider_and_login_loss(self) -> None:
        task = self.create_task("risk-check-owner")
        runner = DolaFetchAutomation(
            task["id"],
            "prompt",
            "9:16",
            account={"cookies": [{"name": "sessionid", "value": "session"}]},
        )

        async def inspect(snapshot: dict, cookies: list[dict] | Exception) -> dict[str, str]:
            page = SimpleNamespace(
                url="https://www.dola.com/chat/local_test",
                wait_for_timeout=AsyncMock(),
                reload=AsyncMock(),
                evaluate=AsyncMock(return_value=snapshot),
            )
            cookie_reader = AsyncMock(side_effect=cookies) if isinstance(cookies, Exception) else AsyncMock(return_value=cookies)
            context = SimpleNamespace(cookies=cookie_reader)
            return await runner._inspect_service_frequent_account_state(page, context)

        slider = asyncio.run(inspect({"sliderVerification": True, "loginInvalid": False, "href": "https://www.dola.com/chat", "bodyText": "请完成验证"}, [{"name": "sessionid"}]))
        login = asyncio.run(inspect({"sliderVerification": False, "loginInvalid": False, "href": "https://www.dola.com/chat", "bodyText": ""}, []))
        normal = asyncio.run(inspect({"sliderVerification": False, "loginInvalid": False, "href": "https://www.dola.com/chat", "bodyText": ""}, [{"name": "sessionid"}]))
        cookie_error = asyncio.run(inspect({"sliderVerification": False, "loginInvalid": False, "href": "https://www.dola.com/chat", "bodyText": ""}, RuntimeError("cookie read failed")))

        self.assertEqual(slider["state"], "slider_verification")
        self.assertEqual(login["state"], "login_invalid")
        self.assertEqual(normal["state"], "service_frequent")
        self.assertEqual(normal["inspection_stage"], "after_reload")
        self.assertEqual(normal["pages_checked"], 1)
        self.assertFalse(normal["page_changed"])
        self.assertEqual(cookie_error["state"], "inspection_failed")
        self.assertEqual(automation.SERVICE_FREQUENT_OBSERVE_DELAY_MS, 15000)

    def test_service_frequent_risk_check_scans_secondary_slider_page_before_reload(self) -> None:
        task = self.create_task("secondary-risk-page")
        runner = DolaFetchAutomation(
            task["id"],
            "prompt",
            "9:16",
            account={"cookies": [{"name": "sessionid", "value": "session"}]},
        )
        main_page = SimpleNamespace(
            url="https://www.dola.com/chat/local_test",
            wait_for_timeout=AsyncMock(),
            reload=AsyncMock(),
            evaluate=AsyncMock(return_value={
                "sliderVerification": False,
                "loginInvalid": False,
                "href": "https://www.dola.com/chat/local_test",
                "bodyText": "",
            }),
        )
        slider_page = SimpleNamespace(
            url="https://verify.dola.com/captcha",
            evaluate=AsyncMock(return_value={
                "sliderVerification": True,
                "loginInvalid": False,
                "href": "https://verify.dola.com/captcha",
                "bodyText": "请完成验证",
            }),
        )
        context = SimpleNamespace(
            pages=[main_page, slider_page],
            cookies=AsyncMock(return_value=[{"name": "sessionid"}]),
        )

        outcome = asyncio.run(runner._inspect_service_frequent_account_state(main_page, context))

        self.assertEqual(outcome["state"], "slider_verification")
        self.assertEqual(outcome["inspection_stage"], "initial")
        self.assertEqual(outcome["pages_checked"], 2)
        main_page.wait_for_timeout.assert_not_awaited()
        main_page.reload.assert_not_awaited()

    def test_service_frequent_waits_fifteen_seconds_for_slider_before_reload(self) -> None:
        task = self.create_task("delayed-slider-page")
        runner = DolaFetchAutomation(
            task["id"],
            "prompt",
            "9:16",
            account={"cookies": [{"name": "sessionid", "value": "session"}]},
        )
        page = SimpleNamespace(
            url="https://www.dola.com/chat/local_test",
            wait_for_timeout=AsyncMock(),
            reload=AsyncMock(),
            evaluate=AsyncMock(side_effect=[
                {
                    "sliderVerification": False,
                    "loginInvalid": False,
                    "href": "https://www.dola.com/chat/local_test",
                    "bodyText": "normal page",
                },
                {
                    "sliderVerification": True,
                    "loginInvalid": False,
                    "href": "https://www.dola.com/chat/local_test",
                    "bodyText": "请完成验证",
                },
            ]),
        )
        context = SimpleNamespace(
            pages=[page],
            cookies=AsyncMock(return_value=[{"name": "sessionid"}]),
        )

        outcome = asyncio.run(runner._inspect_service_frequent_account_state(page, context))

        self.assertEqual(outcome["state"], "slider_verification")
        self.assertEqual(outcome["inspection_stage"], "before_reload")
        page.wait_for_timeout.assert_awaited_once_with(15000)
        page.reload.assert_not_awaited()

    def test_service_frequent_preserves_a_page_that_changes_while_waiting(self) -> None:
        task = self.create_task("changed-risk-page")
        runner = DolaFetchAutomation(
            task["id"],
            "prompt",
            "9:16",
            account={"cookies": [{"name": "sessionid", "value": "session"}]},
        )
        page = SimpleNamespace(
            url="https://www.dola.com/chat/local_test",
            wait_for_timeout=AsyncMock(),
            reload=AsyncMock(),
            evaluate=AsyncMock(side_effect=[
                {
                    "sliderVerification": False,
                    "loginInvalid": False,
                    "href": "https://www.dola.com/chat/local_test",
                    "bodyText": "before",
                },
                {
                    "sliderVerification": False,
                    "loginInvalid": False,
                    "href": "https://www.dola.com/chat/local_test",
                    "bodyText": "after",
                },
            ]),
        )
        context = SimpleNamespace(
            pages=[page],
            cookies=AsyncMock(return_value=[{"name": "sessionid"}]),
        )

        outcome = asyncio.run(runner._inspect_service_frequent_account_state(page, context))

        self.assertEqual(outcome["state"], "service_frequent")
        self.assertEqual(outcome["inspection_stage"], "before_reload")
        self.assertTrue(outcome["page_changed"])
        page.wait_for_timeout.assert_awaited_once_with(15000)
        page.reload.assert_not_awaited()

    def test_login_invalid_retry_disables_and_switches_account(self) -> None:
        task = self.create_task("login-invalid-owner")
        created = accounts.add_account("Login invalid", "session=login-invalid", quota_limit=2)
        claimed = accounts.claim_account_for_worker("worker-login-invalid", task["id"])

        release_account_after_retryable_failure(
            task["id"],
            claimed,
            "dola",
            {
                "retryable": True,
                "account_fault": True,
                "account_login_invalid": True,
                "switch_account": True,
            },
        )

        stored = accounts.list_accounts()[0]
        self.assertFalse(stored["enabled"])
        self.assertEqual(stored["account_status"], "abnormal")
        self.assertEqual(stored["quota_used"], 0)
        self.assertIn(created["id"], store.get_meta(task["id"])["failed_account_ids"])

    def test_doubao_login_invalid_uses_the_correct_platform_label(self) -> None:
        task = store.create_task("豆包测试", "9:16", platform="doubao", model="Seedance 2.0 Mini")
        created = accounts.add_account("Doubao login invalid", "session=doubao-invalid", quota_limit=2, platform="doubao")
        claimed = accounts.claim_account_for_worker("worker-doubao-invalid", task["id"], platform="doubao")

        release_account_after_retryable_failure(
            task["id"],
            claimed,
            "doubao",
            {
                "retryable": True,
                "account_fault": True,
                "account_login_invalid": True,
                "switch_account": True,
            },
        )

        stored = accounts.list_accounts()[0]
        self.assertEqual(stored["account_status"], "abnormal")
        self.assertIn("豆包 登录状态失效", stored["status_reason"])

    def test_slider_verification_streak_restarts_after_a_missed_day(self) -> None:
        created = accounts.add_account("Slider gap", "session=slider-gap", quota_limit=2)
        with patch.object(accounts, "local_today", return_value="2030-02-01"):
            accounts.mark_account_slider_verification(created["id"])
        with patch.object(accounts, "local_today", return_value="2030-02-03"):
            accounts.reset_daily_account_quotas_if_needed()
            repeated = accounts.mark_account_slider_verification(created["id"])
        self.assertEqual(repeated["account_status"], "slider_verification")
        self.assertEqual(repeated["slider_verification_streak"], 1)

    def test_queue_deferral_does_not_consume_generation_retry(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.defer_task(task["id"], "当前用户远端生成任务已达上限，继续排队", "remote_limit", 5)
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta.get("retry_count", 0), 0)
        self.assertEqual(meta["queue_category"], "remote_limit")
        self.assertEqual(meta["error"], "")

    def test_infrastructure_retry_has_separate_budget_and_queue_state(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        count = store.record_infrastructure_retry(task["id"], "Page.goto: net::ERR_PROXY_CONNECTION_FAILED")
        meta = store.get_meta(task["id"])
        self.assertEqual(count, 1)
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta.get("retry_count", 0), 0)
        self.assertEqual(meta["infrastructure_retry_count"], 1)
        self.assertEqual(meta["queue_category"], "infrastructure")
        self.assertEqual(meta["queue_reason"], "服务连接异常，正在恢复")
        self.assertEqual(meta["error"], "")

    def test_retry_attempt_history_preserves_real_backend_errors(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.set_execution_phase(task["id"], "uploading_reference_1", "正在上传参考图（1/1）")
        self.assertEqual(store.record_retry(task["id"], "ApplyImageUpload failed with HTTP 429"), 1)
        self.assertTrue(store.mark_running(task["id"], "worker-2"))
        self.assertEqual(store.record_infrastructure_retry(task["id"], "proxy connection reset"), 1)
        store.mark_failed(task["id"], "多次生成失败")
        history = store.get_meta(task["id"])["attempt_history"]
        self.assertEqual([item["kind"] for item in history], ["execution_retry", "infrastructure_retry", "terminal"])
        self.assertIn("ApplyImageUpload", history[0]["reason"])
        self.assertEqual(history[-1]["reason"], "多次生成失败")

    def test_retry_attempt_history_keeps_repeated_identical_failures(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        self.assertEqual(store.record_retry(task["id"], "相同远端失败"), 1)
        self.assertTrue(store.mark_running(task["id"], "worker-2"))
        self.assertEqual(store.record_retry(task["id"], "相同远端失败"), 2)
        history = store.get_meta(task["id"])["attempt_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual([item["reason"] for item in history], ["相同远端失败", "相同远端失败"])

    def test_supervisor_trims_idle_workers_without_canceling_active_workers(self) -> None:
        manager = WorkerManager()
        manager._workers = {"active-worker": object(), "idle-worker": object()}
        manager._worker_task_ids = {"active-worker": "task-1"}
        self.assertEqual(manager._idle_workers_to_trim(1), ["idle-worker"])
        manager._workers = {"active-worker-1": object(), "active-worker-2": object()}
        manager._worker_task_ids = {"active-worker-1": "task-1", "active-worker-2": "task-2"}
        self.assertEqual(manager._idle_workers_to_trim(1), [])

    def test_browser_timeout_uses_infrastructure_retry_budget(self) -> None:
        task = self.create_task("owner")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertEqual(outcome["reason"], "browser timeout")
        mark_proxy.assert_called_once_with()

    def test_browser_timeout_after_conversation_id_keeps_submission(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.save_result(task["id"], conversation_id="conversation-1")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["success"])
        self.assertFalse(outcome["confirmation_pending"])
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_SUBMITTED)
        result = store.load_result(task["id"])
        self.assertEqual(result["conversation_id"], "conversation-1")
        self.assertTrue(result["post_submission_cleanup_timeout"])
        mark_proxy.assert_not_called()

    def test_browser_timeout_after_ambiguous_submission_enters_recovery(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.save_result(
            task["id"],
            extra={
                "submission_ambiguous": True,
                "submit_confirmation_state": "awaiting_conversation",
            },
        )
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["success"])
        self.assertTrue(outcome["confirmation_pending"])
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_SUBMITTED)
        self.assertTrue(store.load_result(task["id"])["post_submission_cleanup_timeout"])
        mark_proxy.assert_not_called()

    def test_browser_timeout_does_not_treat_pre_submit_marker_as_submission(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.save_result(task["id"], extra={"submission_ambiguous": False})
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError())
        outcome = asyncio.run(runner.run())
        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["reason"], "browser timeout")
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_RUNNING)

    def test_browser_timeout_does_not_treat_rejected_response_as_submission(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.save_result(
            task["id"],
            extra={
                "submission_ambiguous": True,
                "submit_confirmation_state": "awaiting_conversation",
                "submit_error_category": "service_frequent",
            },
        )
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=asyncio.TimeoutError())
        outcome = asyncio.run(runner.run())
        self.assertFalse(outcome["success"])
        self.assertEqual(outcome["reason"], "browser timeout")
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_RUNNING)

    def test_cleanup_timeout_is_suppressed(self) -> None:
        async def never_finishes() -> None:
            await asyncio.Event().wait()

        asyncio.run(automation._bounded_cleanup(never_finishes(), timeout_seconds=0.01))

    def test_navigation_context_loss_is_infrastructure_without_blacking_out_node(self) -> None:
        task = self.create_task("owner")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        reason = "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
        runner._run_once = AsyncMock(side_effect=RuntimeError(reason))
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertEqual(outcome["reason"], reason)
        mark_proxy.assert_not_called()

    def test_tls_alert_is_infrastructure_and_marks_node_unavailable(self) -> None:
        task = self.create_task("owner")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        reason = "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"
        runner._run_once = AsyncMock(side_effect=RuntimeError(reason))
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["infrastructure_fault"])
        mark_proxy.assert_called_once_with()

    def test_ambiguous_submission_retry_does_not_consume_generation_budget(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.mark_submitted(task["id"])
        store.update_meta(
            task["id"],
            preferred_account_id="account-1",
            ambiguous_proxy_retry_count=3,
            ambiguous_proxy_avoid_node_ids=["api:one", "api:two"],
        )
        count = store.retry_ambiguous_submitted_task(task["id"], "页面跳转", max_retries=2, delay_seconds=1)
        meta = store.get_meta(task["id"])
        self.assertEqual(count, 1)
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta["infrastructure_retry_count"], 1)
        self.assertEqual(int(meta.get("retry_count") or 0), 0)
        self.assertEqual(meta["queue_category"], "infrastructure")
        self.assertEqual(meta["submit_phase"], "")
        self.assertEqual(meta["preferred_account_id"], "")
        self.assertEqual(meta["ambiguous_proxy_retry_count"], 0)
        self.assertEqual(meta["ambiguous_proxy_avoid_node_ids"], [])

    def test_ambiguous_proxy_retry_keeps_account_and_does_not_consume_retry_budget(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        store.mark_submitted(task["id"])
        count = store.retry_ambiguous_proxy_task(
            task["id"],
            "提交后未取得有效会话",
            "account-1",
            "api:proxy-one:18001",
            delay_seconds=1,
        )
        meta = store.get_meta(task["id"])
        self.assertEqual(count, 1)
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta["preferred_account_id"], "account-1")
        self.assertEqual(meta["ambiguous_proxy_retry_count"], 1)
        self.assertEqual(meta["ambiguous_proxy_avoid_node_ids"], ["api:proxy-one:18001"])
        self.assertEqual(int(meta.get("infrastructure_retry_count") or 0), 0)
        self.assertEqual(int(meta.get("retry_count") or 0), 0)
        self.assertEqual(meta["queue_category"], "proxy_refresh")

    def test_reference_attachment_cache_reuses_identical_images_for_same_account(self) -> None:
        automation.clear_reference_attachment_cache()
        first = self.create_task("owner-a")
        second = self.create_task("owner-b")
        for task in (first, second):
            image = store.images_dir(task["id"]) / "01.png"
            image.write_bytes(b"same-reference-image")
            store.set_task_images(task["id"], [image])
        attachment = {"uri": "tos/example", "name": "01.png", "width": 10, "height": 10, "size": 20, "mime": "image/png"}
        upload = AsyncMock(return_value=attachment)

        async def run() -> None:
            account = {"id": "dola-account-a"}
            first_runner = DolaFetchAutomation(first["id"], "first", "9:16", account=account)
            second_runner = DolaFetchAutomation(second["id"], "second", "9:16", account=account)
            first_runner._upload_one_image_by_fetch = upload
            second_runner._upload_one_image_by_fetch = upload
            self.assertEqual(await first_runner._upload_images_if_needed(object()), [attachment])
            self.assertEqual(await second_runner._upload_images_if_needed(object()), [attachment])

        asyncio.run(run())
        self.assertEqual(upload.await_count, 1)
        second_result = store.load_result(second["id"])
        self.assertEqual(second_result["reference_image_cache_hits"], 1)
        self.assertEqual(second_result["reference_image_cache_misses"], 0)

        store.update_meta(second["id"], reference_upload_cache_bypass=True)
        asyncio.run(run())
        self.assertEqual(upload.await_count, 2)
        self.assertFalse(store.get_meta(second["id"])["reference_upload_cache_bypass"])
        automation.clear_reference_attachment_cache()

    def test_reference_attachment_cache_does_not_cross_accounts(self) -> None:
        automation.clear_reference_attachment_cache()
        first = self.create_task("owner-a")
        second = self.create_task("owner-b")
        for task in (first, second):
            image = store.images_dir(task["id"]) / "01.png"
            image.write_bytes(b"same-reference-image")
            store.set_task_images(task["id"], [image])
        first_attachment = {"uri": "tos/account-a", "name": "01.png"}
        second_attachment = {"uri": "tos/account-b", "name": "01.png"}
        upload = AsyncMock(side_effect=[first_attachment, second_attachment])

        async def run() -> None:
            first_runner = DolaFetchAutomation(first["id"], "first", "9:16", account={"id": "dola-account-a"})
            second_runner = DolaFetchAutomation(second["id"], "second", "9:16", account={"id": "dola-account-b"})
            first_runner._upload_one_image_by_fetch = upload
            second_runner._upload_one_image_by_fetch = upload
            self.assertEqual(await first_runner._upload_images_if_needed(object()), [first_attachment])
            self.assertEqual(await second_runner._upload_images_if_needed(object()), [second_attachment])

        asyncio.run(run())
        self.assertEqual(upload.await_count, 2)
        self.assertEqual(store.load_result(second["id"])["reference_image_cache_hits"], 0)
        automation.clear_reference_attachment_cache()

    def test_reference_attachment_cache_does_not_cross_login_sessions(self) -> None:
        automation.clear_reference_attachment_cache()
        first = self.create_task("owner-a")
        second = self.create_task("owner-b")
        for task in (first, second):
            image = store.images_dir(task["id"]) / "01.png"
            image.write_bytes(b"same-reference-image")
            store.set_task_images(task["id"], [image])
        upload = AsyncMock(side_effect=[{"uri": "tos/session-a"}, {"uri": "tos/session-b"}])

        async def run() -> None:
            first_runner = DolaFetchAutomation(first["id"], "first", "9:16", account={"id": "dola-account-a", "cookies": [{"name": "session", "value": "a"}]})
            second_runner = DolaFetchAutomation(second["id"], "second", "9:16", account={"id": "dola-account-a", "cookies": [{"name": "session", "value": "b"}]})
            first_runner._upload_one_image_by_fetch = upload
            second_runner._upload_one_image_by_fetch = upload
            await first_runner._upload_images_if_needed(object())
            await second_runner._upload_images_if_needed(object())

        asyncio.run(run())
        self.assertEqual(upload.await_count, 2)
        automation.clear_reference_attachment_cache()

    def test_reference_upload_slot_wraps_only_the_real_network_upload(self) -> None:
        automation.clear_reference_attachment_cache()
        task = self.create_task("owner-upload-slot")
        image = store.images_dir(task["id"]) / "01.png"
        image.write_bytes(b"upload-slot-reference")
        store.set_task_images(task["id"], [image])
        events: list[str] = []
        attachment = {"uri": "tos/upload-slot", "name": "01.png"}

        @asynccontextmanager
        async def upload_slot():
            events.append("slot-enter")
            try:
                yield
            finally:
                events.append("slot-exit")

        async def upload(_page, _path):
            self.assertEqual(events, ["slot-enter"])
            events.append("network-upload")
            return attachment

        async def run() -> None:
            runner = DolaFetchAutomation(task["id"], "prompt", "9:16", account={"id": "dola-upload-slot"}, image_upload_slot=upload_slot)
            runner._upload_one_image_by_fetch = upload
            self.assertEqual(await runner._upload_images_if_needed(object()), [attachment])

        asyncio.run(run())
        self.assertEqual(events, ["slot-enter", "network-upload", "slot-exit"])
        automation.clear_reference_attachment_cache()

    def test_worker_image_upload_slot_enforces_eight_active_uploads(self) -> None:
        manager = WorkerManager()
        entered = 0
        maximum = 0
        eight_entered = asyncio.Event()
        release = asyncio.Event()

        async def exercise() -> None:
            nonlocal entered, maximum

            async def hold(index: int) -> None:
                nonlocal entered, maximum
                async with manager._image_upload_slot(f"{index:032x}", f"owner-{index}"):
                    entered += 1
                    maximum = max(maximum, entered)
                    if entered == IMAGE_SUBMISSION_CONCURRENCY:
                        eight_entered.set()
                    await release.wait()
                    entered -= 1

            with patch.object(manager, "_image_owner_limit", return_value=IMAGE_SUBMISSION_CONCURRENCY), patch(
                "app.worker.set_execution_phase"
            ):
                tasks = [asyncio.create_task(hold(index)) for index in range(IMAGE_SUBMISSION_CONCURRENCY + 1)]
                await asyncio.wait_for(eight_entered.wait(), timeout=2)
                await asyncio.sleep(0)
                self.assertEqual(manager._image_submission_active, IMAGE_SUBMISSION_CONCURRENCY)
                self.assertEqual(len(manager._image_submission_reservations), IMAGE_SUBMISSION_CONCURRENCY)
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

        asyncio.run(exercise())
        self.assertEqual(maximum, IMAGE_SUBMISSION_CONCURRENCY)
        self.assertEqual(manager._image_submission_active, 0)
        self.assertEqual(manager._image_submission_reservations, {})

    def test_image_upload_slot_timeout_defers_without_leaking_reservation(self) -> None:
        manager = WorkerManager()
        manager._image_submission_reservations = {
            f"existing-{index}": f"owner-{index}"
            for index in range(IMAGE_SUBMISSION_CONCURRENCY)
        }

        async def exercise() -> None:
            with patch.object(manager, "_image_owner_limit", return_value=IMAGE_SUBMISSION_CONCURRENCY), patch(
                "app.worker.IMAGE_UPLOAD_SLOT_WAIT_SECONDS",
                0.01,
            ), patch("app.worker.set_execution_phase"):
                with self.assertRaises(automation.ReferenceUploadCapacityError):
                    async with manager._image_upload_slot("waiting-task", "waiting-owner"):
                        self.fail("busy upload slot should not be entered")

        asyncio.run(exercise())
        self.assertNotIn("waiting-task", manager._image_submission_reservations)
        self.assertEqual(manager._image_submission_active, 0)

    def test_reference_image_upload_has_a_total_timeout_and_releases_slot(self) -> None:
        automation.clear_reference_attachment_cache()
        task = self.create_task("owner-upload-timeout")
        image = store.images_dir(task["id"]) / "01.png"
        image.write_bytes(b"upload-timeout-reference")
        store.set_task_images(task["id"], [image])
        slot_exited = False

        @asynccontextmanager
        async def upload_slot():
            nonlocal slot_exited
            try:
                yield
            finally:
                slot_exited = True

        async def hanging_upload(_page, _path):
            await asyncio.Event().wait()

        async def exercise() -> None:
            runner = DolaFetchAutomation(
                task["id"],
                "prompt",
                "9:16",
                account={"id": "dola-upload-timeout"},
                image_upload_slot=upload_slot,
            )
            runner._upload_one_image_by_fetch = hanging_upload
            with patch.object(automation, "REFERENCE_IMAGE_UPLOAD_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(RuntimeError, "reference image upload timed out"):
                    await runner._upload_images_if_needed(object())

        asyncio.run(exercise())
        self.assertTrue(slot_exited)
        self.assertEqual(automation._REFERENCE_UPLOADS_IN_FLIGHT, {})
        automation.clear_reference_attachment_cache()

    def test_prepare_upload_timeout_is_reported_as_reference_upload_failure(self) -> None:
        async def hanging_evaluate(*_args):
            await asyncio.Event().wait()

        page = SimpleNamespace(evaluate=AsyncMock(side_effect=hanging_evaluate))

        async def exercise() -> None:
            runner = DolaFetchAutomation("missing-task", "prompt", "9:16")
            with patch.object(automation, "PREPARE_UPLOAD_TIMEOUT_SECONDS", 0.01):
                with self.assertRaisesRegex(RuntimeError, "prepare_upload timed out"):
                    await runner._prepare_image_upload(page)

        asyncio.run(exercise())

    def test_reference_upload_timeout_uses_infrastructure_retry_budget(self) -> None:
        task = self.create_task("owner-reference-infrastructure")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner._run_once = AsyncMock(side_effect=RuntimeError("reference image upload timed out"))
        outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertEqual(store.load_result(task["id"])["submit_error_category"], "reference_upload")

    def test_reference_upload_timeout_invalidates_only_the_active_api_proxy(self) -> None:
        task = self.create_task("owner-reference-api-timeout")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner.active_proxy_source = "api"
        runner._run_once = AsyncMock(side_effect=RuntimeError("prepare_upload timed out"))
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["retryable"])
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertFalse(outcome.get("defer_only", False))
        mark_proxy.assert_called_once_with(reason="reference_upload_timeout")

    def test_navigation_timeout_is_a_proxy_transport_failure(self) -> None:
        task = self.create_task("owner-navigation-timeout")
        runner = DolaFetchAutomation(task["id"], "prompt", "9:16")
        runner.active_proxy_source = "api"
        runner._run_once = AsyncMock(side_effect=RuntimeError("Page.goto: net::ERR_TIMED_OUT"))
        with patch.object(runner, "_mark_active_proxy_unavailable") as mark_proxy:
            outcome = asyncio.run(runner.run())
        self.assertTrue(outcome["infrastructure_fault"])
        self.assertTrue(outcome["defer_only"])
        self.assertEqual(outcome["defer_category"], "proxy_refresh")
        mark_proxy.assert_called_once_with()

    def test_reference_attachment_cache_coalesces_concurrent_uploads(self) -> None:
        automation.clear_reference_attachment_cache()
        first = self.create_task("owner-a")
        second = self.create_task("owner-b")
        for task in (first, second):
            image = store.images_dir(task["id"]) / "01.png"
            image.write_bytes(b"same-reference-image")
            store.set_task_images(task["id"], [image])
        attachment = {"uri": "tos/shared", "name": "01.png"}
        upload_started = asyncio.Event()
        release_upload = asyncio.Event()
        second_waiting = asyncio.Event()
        upload_count = 0

        async def upload_once(_page, _path):
            nonlocal upload_count
            upload_count += 1
            upload_started.set()
            await release_upload.wait()
            return attachment

        async def run() -> None:
            account = {"id": "dola-account-a"}
            first_runner = DolaFetchAutomation(first["id"], "first", "9:16", account=account)
            second_runner = DolaFetchAutomation(second["id"], "second", "9:16", account=account)
            first_runner._upload_one_image_by_fetch = upload_once
            second_runner._upload_one_image_by_fetch = upload_once
            original_set_phase = second_runner._set_phase

            def track_second_phase(phase, status_reason):
                original_set_phase(phase, status_reason)
                if phase.startswith("waiting_reference_"):
                    second_waiting.set()

            second_runner._set_phase = track_second_phase
            first_upload = asyncio.create_task(first_runner._upload_images_if_needed(object()))
            await upload_started.wait()
            second_upload = asyncio.create_task(second_runner._upload_images_if_needed(object()))
            await second_waiting.wait()
            release_upload.set()
            self.assertEqual(await first_upload, [attachment])
            self.assertEqual(await second_upload, [attachment])

        asyncio.run(run())
        self.assertEqual(upload_count, 1)
        second_result = store.load_result(second["id"])
        self.assertEqual(second_result["reference_image_cache_waits"], 1)
        self.assertEqual(second_result["reference_image_cache_hits"], 1)
        self.assertEqual(second_result["reference_image_cache_misses"], 0)
        automation.clear_reference_attachment_cache()

    def test_worker_has_independent_image_submission_limit(self) -> None:
        manager = WorkerManager()
        self.assertEqual(IMAGE_SUBMISSION_CONCURRENCY, 8)
        self.assertEqual(manager._image_submission_semaphore._value, 8)
        self.assertEqual(manager._image_submission_reservations, {})
        snapshot = manager.health_snapshot()
        self.assertEqual(snapshot["image_upload_concurrency"], 8)
        self.assertEqual(snapshot["image_upload_reserved"], 0)
        self.assertEqual(snapshot["image_submission_claimed"], 0)
        self.assertEqual(snapshot["image_submission_claim_limit"], IMAGE_PREPARATION_CONCURRENCY)
        self.assertEqual(snapshot["image_preparation_claimed"], 0)
        self.assertEqual(snapshot["image_preparation_claim_limit"], 8)
        self.assertEqual(snapshot["browser_pool"]["process_limit"], 12)
        self.assertEqual(snapshot["browser_pool"]["contexts_per_process"], 3)
        self.assertEqual(snapshot["browser_pool"]["submission_capacity"], 36)
        self.assertEqual(snapshot["api_proxy_pool"]["capacity"], 12)
        self.assertEqual(snapshot["api_proxy_pool"]["refresh_concurrency_limit"], 2)

    def test_reference_preparation_releases_before_submission_and_only_once(self) -> None:
        manager = WorkerManager()
        task_id = "a" * 32
        manager._claimed_image_preparations[task_id] = "owner-a"
        runner = DolaFetchAutomation(
            task_id,
            "prompt",
            "9:16",
            image_preparation_done=lambda: manager._release_image_preparation(task_id),
        )
        runner._finish_image_preparation()
        runner._finish_image_preparation()
        self.assertNotIn(task_id, manager._claimed_image_preparations)

    def test_fair_owner_limits_split_capacity_across_concurrent_members(self) -> None:
        self.assertEqual(
            fair_owner_capacity_limits({"owner-a": 20, "owner-b": 20, "owner-c": 20}, 48),
            {"owner-a": 16, "owner-b": 16, "owner-c": 16},
        )
        self.assertEqual(
            fair_owner_capacity_limits({"owner-a": 1, "owner-b": 20, "owner-c": 20}, 48),
            {"owner-a": 1, "owner-b": 20, "owner-c": 20},
        )

    def test_worker_applies_fair_browser_and_image_limits_per_owner(self) -> None:
        manager = WorkerManager()
        rows = [
            (f"task-{owner}", {"status": "pending", "platform": "dola", "owner_token_hash": owner, "image_count": 1})
            for owner in ("owner-a", "owner-b", "owner-c")
        ]

        async def calculate() -> None:
            with patch("app.worker.temp_token_concurrency_limits", return_value={"owner-a": 20, "owner-b": 20, "owner-c": 20}), patch(
                "app.worker.list_task_metas_by_statuses", return_value=rows
            ):
                self.assertEqual(manager._owner_concurrency_limits(), {"owner-a": 12, "owner-b": 12, "owner-c": 12})
                self.assertEqual(
                    {owner: manager._image_owner_limit(owner) for owner in ("owner-a", "owner-b", "owner-c")},
                    {"owner-a": 3, "owner-b": 3, "owner-c": 2},
                )

        asyncio.run(calculate())

    def test_reconciliation_repairs_quota_used_from_charge_ledger(self) -> None:
        created = accounts.add_account("Dola", "session=value", quota_limit=3)
        claimed = accounts.claim_account_for_worker("worker-1", "task-1")
        self.assertIsNotNone(claimed)
        data = json.loads(self.accounts_path.read_text(encoding="utf-8"))
        data["accounts"][0]["quota_used"] = 0
        self.accounts_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(accounts.reconcile_account_quotas(), {"checked": 1, "repaired": 1})
        self.assertEqual(accounts.list_accounts()[0]["quota_used"], 1)

    def test_generating_result_keeps_polling_same_remote_task_before_deadline(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account1", "account_quota_charge_id": "charge1"})
        store.mark_submitted(task["id"])
        store.update_meta(task["id"], submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=19)).isoformat())
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task") as clear_account, patch(
            "app.worker.settle_account_quota"
        ) as settle_account, patch("app.worker.refund_temp_quota_once") as refund_owner, patch(
            "app.worker.query_task", new=AsyncMock(return_value={"code": "1", "text": "生成中", "url": ""})
        ) as query:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_SUBMITTED)
        self.assertEqual(meta["execution_phase"], "waiting_result_low_rate")
        self.assertTrue(meta.get("late_account_released_at"))
        self.assertNotIn("result_timeout_retry_count", meta)
        self.assertNotIn("failed_account_ids", meta)
        query.assert_awaited_once_with(task["id"])
        clear_account.assert_called_once_with("account1", task["id"])
        settle_account.assert_not_called()
        refund_owner.assert_not_called()
        self.assertEqual(store.load_result(task["id"])["account_id"], "account1")

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

    def test_proxy_cooldown_defer_does_not_consume_retry_budget(self) -> None:
        task = self.create_task("owner")
        self.assertTrue(store.mark_running(task["id"], "worker-1"))
        self.assertTrue(defer_non_counting_retry(task["id"], {"defer_only": True, "retry_after": 45}))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_PENDING)
        self.assertEqual(meta.get("retry_count", 0), 0)
        self.assertEqual(meta.get("infrastructure_retry_count", 0), 0)
        self.assertEqual(meta["queue_category"], "proxy_cooldown")
        self.assertGreaterEqual(datetime.fromisoformat(meta["next_attempt_at"]), datetime.now(timezone.utc) + timedelta(seconds=40))

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

    def test_twenty_minute_timeout_does_not_requeue_after_cancel_request(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account1", "account_quota_charge_id": "charge1"})
        store.mark_submitted(task["id"])
        store.update_meta(task["id"], submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat(), cancel_requested=True)
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task") as clear_account, patch("app.worker.settle_account_quota") as settle_account, patch(
            "app.worker.refund_temp_quota_once"
        ) as refund_owner:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertNotIn("result_timeout_retry_count", meta)
        self.assertEqual(meta["error"], "生成超过20分钟，仍未返回结果")
        clear_account.assert_called_once_with("account1", task["id"])
        settle_account.assert_called_once_with("account1", "charge1")
        refund_owner.assert_called_once_with(task["id"], "owner")
        self.assertEqual(meta["execution_phase"], "late_result_watch")
        late_until = datetime.fromisoformat(meta["late_result_watch_until"])
        self.assertGreater(late_until, datetime.now(timezone.utc) + timedelta(minutes=8))

    def test_late_result_observation_can_recover_video_before_thirty_minutes(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account-late", "account_quota_charge_id": "charge-late"})
        store.mark_submitted(task["id"])
        store.update_meta(task["id"], submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat())
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task"), patch("app.worker.settle_account_quota"), patch("app.worker.refund_temp_quota_once"):
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_FAILED)
        store.save_result(task["id"], extra={"decoded_main_url": "https://cdn.example/late.mp4"})
        asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_SUCCESS)
        self.assertEqual(meta.get("late_result_watch_until"), "")

    def test_twenty_minute_timeout_fails_once_without_resubmission(self) -> None:
        task = self.create_task("owner")
        store.mark_running(task["id"], "worker-1")
        store.save_result(task["id"], extra={"account_id": "account2", "account_quota_charge_id": "charge2"})
        store.mark_submitted(task["id"])
        store.update_meta(
            task["id"],
            submitted_at=(datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat(),
        )
        manager = WorkerManager()
        with patch("app.worker.clear_account_current_task") as clear_account, patch(
            "app.worker.settle_account_quota"
        ) as settle_account, patch("app.worker.refund_temp_quota_once") as refund_owner, patch(
            "app.worker.query_task", new=AsyncMock()
        ) as query:
            asyncio.run(manager._watch_unfinished_success_tasks([task["id"]]))
        meta = store.get_meta(task["id"])
        self.assertEqual(meta["status"], store.STATUS_FAILED)
        self.assertEqual(meta["error"], "生成超过20分钟，仍未返回结果")
        self.assertEqual(int(meta.get("retry_count") or 0), 0)
        self.assertNotIn("result_timeout_retry_count", meta)
        self.assertNotIn("failed_account_ids", meta)
        clear_account.assert_called_once_with("account2", task["id"])
        settle_account.assert_called_once_with("account2", "charge2")
        refund_owner.assert_called_once_with(task["id"], "owner")
        query.assert_not_awaited()

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

    def test_pending_retry_wait_does_not_consume_thirty_minute_execution_timeout(self) -> None:
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
        self.assertEqual(listed["status"], store.STATUS_PENDING)
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).free_remaining, 0)

        self.assertTrue(store.mark_running(task["id"], "worker-retry-timeout"))
        store.update_meta(
            task["id"],
            retry_attempt_started_at=(datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),
        )
        listed = next(item for item in store.list_tasks() if item["id"] == task["id"])
        self.assertEqual(listed["status"], store.STATUS_FAILED)
        self.assertEqual(listed["error"], "重试超过30分钟，生成失败")
        self.assertEqual(temp_access.get_temp_context_by_hash(owner_hash).free_remaining, 1)

    def test_file_queue_claims_originally_older_task_after_it_is_deferred(self) -> None:
        older = self.create_task("owner-oldest")
        newer = self.create_task("owner-newest")
        old_priority = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        store.update_meta(older["id"], created_at=old_priority, queue_priority_at=old_priority)
        store.defer_task(older["id"], "稍后重试", "test", 1)
        store.update_meta(older["id"], next_attempt_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())

        claimed = store.claim_next_pending("fifo-worker", set())
        self.assertEqual(claimed, older["id"])
        self.assertEqual(store.get_meta(newer["id"])["status"], store.STATUS_PENDING)

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

    def test_configured_retry_limit_controls_real_task_retries(self) -> None:
        task = self.create_task("owner")
        with patch.object(store, "load_settings", return_value=SimpleNamespace(task_retry_limit=4)):
            for expected in range(1, 5):
                self.assertTrue(store.mark_running(task["id"], f"worker-{expected}"))
                self.assertEqual(store.record_retry(task["id"], f"failure-{expected}"), expected)
                self.assertEqual(store.get_meta(task["id"])["status"], store.STATUS_PENDING)
            self.assertTrue(store.mark_running(task["id"], "worker-5"))
            self.assertEqual(store.record_retry(task["id"], "failure-5"), 5)
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
            with patch("app.worker.reset_running_tasks") as reset, patch("app.worker.queue_backend", return_value="file"), patch.object(
                manager._dola_browser_pool, "start", new=AsyncMock()
            ) as pool_start, patch.object(manager._dola_browser_pool, "stop", new=AsyncMock()) as pool_stop:
                await manager.start()
                supervisor = manager._supervisor
                watchdog = manager._watchdog
                await manager.start()
                self.assertIs(manager._supervisor, supervisor)
                self.assertIs(manager._watchdog, watchdog)
                reset.assert_called_once()
                await manager.stop()
                pool_start.assert_awaited_once()
                pool_stop.assert_awaited_once()

        asyncio.run(start_twice())

    def test_remote_generation_limit_is_enforced_per_user(self) -> None:
        manager = WorkerManager()
        submitted = [("submitted-task", {"status": "submitted", "platform": "dola", "owner_token_hash": "owner-a"})]

        async def check_limits() -> None:
            with patch("app.worker.list_task_metas_by_statuses", return_value=submitted), patch(
                "app.worker.temp_token_remote_generation_limits", return_value={"owner-a": 2, "owner-b": 1}
            ):
                self.assertTrue(manager._reserve_remote_generation_slot("new-task-1", {"owner_token_hash": "owner-a"}))
                self.assertFalse(manager._reserve_remote_generation_slot("new-task-2", {"owner_token_hash": "owner-a"}))
                self.assertTrue(manager._reserve_remote_generation_slot("other-user-task", {"owner_token_hash": "owner-b"}))

        asyncio.run(check_limits())
        manager._remote_generation_reservations.pop("new-task-1")
        manager._remote_generation_reservations.pop("other-user-task")
        self.assertEqual(manager._remote_generation_reservations, {})

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
