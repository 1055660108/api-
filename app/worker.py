from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from .accounts import account_for_current_task, claim_account_for_worker, clear_account_current_task, exhaust_timed_out_account, refund_account_quota, settle_account_quota
from .automation import DolaFetchAutomation, is_final_generation_failure
from .doubao_automation import DoubaoVideoAutomation
from .qianwen_automation import QianwenVideoAutomation
from .config import load_settings
from .memory import reclaim_memory_after_task
from .store import (
    claim_next_pending,
    count_pending_tasks,
    can_run_task,
    clear_transient_result,
    get_meta,
    has_pending_tasks,
    expire_task_if_timeout,
    is_task_canceled,
    list_tasks,
    load_result,
    mark_account_refund_once,
    mark_failed,
    mark_pending,
    mark_submitted,
    mark_result_once,
    MAX_TASK_RETRIES,
    record_failed_account,
    record_execution_miss,
    record_result_watch_miss,
    record_retry,
    reset_running_tasks,
    retry_timed_out_submitted_task,
    set_active_tasks,
    STATUS_SUBMITTED,
    update_meta,
)
from .query import query_task
from .resilience import PlatformGuard, adaptive_worker_limit
from .task_queue import get_task_queue, queue_backend
from .temp_access import refund_temp_quota_hash
from .temp_access import temp_token_concurrency_limits


DOLA_SUBMIT_INTERVAL_SECONDS = 5.0
GENERATING_TEXT = "正在为您生成视频，请稍候...本次使用 Seedance 2.0生成，预计等待 3~8 分钟。"
RUNNING_WATCH_GRACE_SECONDS = 90
SUCCESS_WATCH_GRACE_SECONDS = 120
RESULT_WATCH_DEADLINE_MINUTES = 8
RETRY_ACCOUNT_WAIT_MINUTES = 5


def refund_temp_quota_once(task_id: str, owner_hash: str) -> None:
    if owner_hash and refund_temp_quota_hash(owner_hash, task_id):
        mark_result_once(task_id, "temp_quota_refunded", True)


def refund_account_quota_once(task_id: str, account_id: str, charge_id: str = "") -> None:
    if account_id and refund_account_quota(account_id, charge_id or task_id):
        mark_account_refund_once(task_id, account_id)


def consume_failed_account_quota(task_id: str, account: dict, platform: str) -> None:
    account_id = str(account.get("id") or "")
    charge_id = str(account.get("quota_charge_id") or "")
    if not account_id:
        return
    if platform == "dola":
        settle_account_quota(account_id, charge_id)
    else:
        refund_account_quota_once(task_id, account_id, charge_id)


class WorkerManager:
    def __init__(self) -> None:
        self._supervisor: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._workers: dict[str, asyncio.Task] = {}
        self._worker_task_ids: dict[str, str] = {}
        self._claim_lock = asyncio.Lock()
        self._dola_submit_lock = asyncio.Lock()
        self._last_dola_submit_at = 0.0
        self._claimed: set[str] = set()
        self._stopping = False
        self._worker_seq = 0
        self._instance_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
        self._restart_count = 0
        self._last_error = ""
        self._queue = get_task_queue()
        self._platform_guard = PlatformGuard(getattr(self._queue, "client", None))
        self._resource_snapshot: dict[str, object] = {}

    async def start(self) -> None:
        if self._supervisor and not self._supervisor.done():
            return
        if queue_backend() == "file":
            reset_running_tasks()
        else:
            self._queue.recover()
        self._queue.reconcile()
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise())
        self._watchdog = asyncio.create_task(self._watch_running_tasks())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if self._supervisor:
            self._supervisor.cancel()
        if self._watchdog:
            self._watchdog.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._supervisor:
            await asyncio.gather(self._supervisor, return_exceptions=True)
        if self._watchdog:
            await asyncio.gather(self._watchdog, return_exceptions=True)
        self._claimed.clear()
        self._worker_task_ids.clear()
        set_active_tasks([])

    def health_snapshot(self) -> dict:
        configured = load_settings().browser_workers
        effective, resource = adaptive_worker_limit(configured)
        supervisor_alive = bool(self._supervisor and not self._supervisor.done())
        watchdog_alive = bool(self._watchdog and not self._watchdog.done())
        worker_alive = sum(1 for task in self._workers.values() if not task.done())
        healthy = supervisor_alive and watchdog_alive and worker_alive >= 1
        return {
            "ok": healthy,
            "supervisor_alive": supervisor_alive,
            "watchdog_alive": watchdog_alive,
            "worker_alive": worker_alive,
            "worker_configured": configured,
            "worker_effective": effective,
            "claimed": len(self._claimed),
            "restart_count": self._restart_count,
            "last_error": self._last_error,
            "resources": resource,
        }

    def cancel_task(self, task_id: str) -> bool:
        task_id = str(task_id or "")
        for worker_id, current_task_id in list(self._worker_task_ids.items()):
            if current_task_id != task_id:
                continue
            task = self._workers.get(worker_id)
            if task and not task.done():
                task.cancel()
                return True
        return False

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                if not self._watchdog or self._watchdog.done():
                    if self._watchdog:
                        with suppress(asyncio.CancelledError, Exception):
                            error = self._watchdog.exception()
                            if error:
                                self._last_error = str(error)[:500]
                    self._restart_count += 1
                    self._watchdog = asyncio.create_task(self._watch_running_tasks())
                for worker_id, task in list(self._workers.items()):
                    if task.done():
                        with suppress(asyncio.CancelledError, Exception):
                            error = task.exception()
                            if error:
                                self._last_error = str(error)[:500]
                        self._workers.pop(worker_id, None)
                configured = load_settings().browser_workers
                effective, self._resource_snapshot = adaptive_worker_limit(configured)
                self._queue.heartbeat({task_id: worker_id for worker_id, task_id in self._worker_task_ids.items()})
                demand = len(self._claimed)
                with suppress(Exception):
                    demand += count_pending_tasks()
                desired = min(effective, max(1, demand))
                current_ids = list(self._workers.keys())
                for worker_id in current_ids[desired:]:
                    task = self._workers.pop(worker_id, None)
                    if task:
                        task.cancel()
                while len(self._workers) < desired:
                    self._worker_seq += 1
                    worker_id = f"{self._instance_id}-{self._worker_seq}"
                    self._workers[worker_id] = asyncio.create_task(self._worker_loop(worker_id))
                set_active_tasks(self._claimed)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)[:500]
                self._restart_count += 1
                await asyncio.sleep(2)

    async def _watch_running_tasks(self) -> None:
        while not self._stopping:
            await asyncio.sleep(60)
            try:
                await self._watch_running_tasks_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)[:500]
                self._restart_count += 1

    async def _watch_running_tasks_once(self) -> None:
        running_ids: list[str] = []
        success_ids: list[str] = []
        for item in list_tasks():
            task_id = str(item.get("id") or "")
            status = str(item.get("status") or "")
            if task_id and status == "running":
                running_ids.append(task_id)
            if task_id and status == STATUS_SUBMITTED:
                success_ids.append(task_id)
        await self._watch_unfinished_success_tasks(success_ids)
        if queue_backend() == "redis":
            return
        for task_id in running_ids:
            with suppress(FileNotFoundError):
                meta = get_meta(task_id)
                if str(meta.get("status") or "") != "running":
                    continue
                started_at = self._parse_utc(str(meta.get("started_at") or meta.get("updated_at") or ""))
                if started_at and datetime.now(timezone.utc) - started_at < timedelta(seconds=RUNNING_WATCH_GRACE_SECONDS):
                    continue
                worker_id = str(meta.get("worker_id") or "")
                task = self._workers.get(worker_id) if worker_id else None
                if account_for_current_task(task_id) and task and not task.done():
                    continue
                miss_count = record_execution_miss(task_id)
                self._claimed.discard(task_id)
                if worker_id:
                    if task and not task.done():
                        task.cancel()
                    self._worker_task_ids.pop(worker_id, None)
                if miss_count > MAX_TASK_RETRIES:
                    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        set_active_tasks(self._claimed)

    async def _watch_unfinished_success_tasks(self, task_ids: list[str]) -> None:
        for task_id in task_ids:
            with suppress(FileNotFoundError):
                result = load_result(task_id)
                if result.get("decoded_main_url"):
                    continue
                meta = get_meta(task_id)
                submitted_at = self._parse_utc(str(meta.get("submitted_at") or meta.get("updated_at") or ""))
                if submitted_at and datetime.now(timezone.utc) - submitted_at >= timedelta(minutes=RESULT_WATCH_DEADLINE_MINUTES):
                    account_id = str(result.get("account_id") or "")
                    if account_id:
                        exhaust_timed_out_account(account_id, str(result.get("account_quota_charge_id") or ""))
                        clear_account_current_task(account_id, task_id)
                    if not bool(meta.get("cancel_requested")):
                        if account_id:
                            record_failed_account(task_id, account_id)
                        retry_count = retry_timed_out_submitted_task(task_id, "生成超过8分钟，正在重试", max_retries=MAX_TASK_RETRIES)
                        if retry_count <= MAX_TASK_RETRIES:
                            clear_transient_result(task_id)
                            continue
                    mark_failed(task_id, "生成超过8分钟，两次重试后仍未返回结果")
                    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                    continue
                finished_at = self._parse_utc(str(meta.get("finished_at") or meta.get("updated_at") or ""))
                if finished_at and datetime.now(timezone.utc) - finished_at < timedelta(seconds=SUCCESS_WATCH_GRACE_SECONDS):
                    continue
                outcome = await query_task(task_id)
                if str(outcome.get("code") or "") == "2":
                    continue
                text = str(outcome.get("text") or "")
                if text and text not in {"没有文本", GENERATING_TEXT}:
                    continue
                record_result_watch_miss(task_id)

    def _parse_utc(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _retry_account_wait_expired(self, meta: dict) -> bool:
        if max(0, int(meta.get("result_timeout_retry_count") or 0)) < 1:
            return False
        queued_at = self._parse_utc(str(meta.get("retry_queued_at") or meta.get("next_attempt_at") or ""))
        return bool(queued_at and datetime.now(timezone.utc) - queued_at >= timedelta(minutes=RETRY_ACCOUNT_WAIT_MINUTES))

    def _handle_unavailable_account(self, task_id: str, meta: dict, platform: str) -> bool:
        if self._retry_account_wait_expired(meta):
            mark_failed(task_id, "重试等待可用账号超时，请重新提交")
            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
            return True
        mark_pending(task_id, f"等待可用{platform}账号")
        return False

    async def _worker_loop(self, worker_id: str) -> None:
        while not self._stopping:
            async with self._claim_lock:
                active_counts: dict[str, int] = {}
                for claimed_id in self._claimed:
                    with suppress(FileNotFoundError):
                        owner = str(get_meta(claimed_id).get("owner_token_hash") or "")
                        if owner:
                            active_counts[owner] = active_counts.get(owner, 0) + 1
                task_id = self._queue.claim(worker_id, self._claimed, active_counts, temp_token_concurrency_limits())
                if task_id:
                    self._claimed.add(task_id)
                    self._worker_task_ids[worker_id] = task_id
            if not task_id:
                await asyncio.sleep(2)
                continue
            set_active_tasks(self._claimed)
            account = None
            try:
                meta = get_meta(task_id)
                if expire_task_if_timeout(task_id):
                    continue
                if is_task_canceled(task_id):
                    continue
                failed_account_ids = set(str(item) for item in meta.get("failed_account_ids") or [] if item)
                platform = str(meta.get("platform") or "dola")
                if platform not in {"dola", "doubao", "qianwen"}:
                    mark_failed(task_id, "该平台网页自动化暂未接入")
                    continue
                if not can_run_task(task_id, worker_id):
                    continue
                account = claim_account_for_worker(worker_id, task_id, exclude_ids=failed_account_ids, platform=platform)
                if not account:
                    if not self._handle_unavailable_account(task_id, meta, platform):
                        await asyncio.sleep(3)
                    continue
                if meta.get("retry_queued_at"):
                    update_meta(task_id, retry_queued_at="")
                admission = self._platform_guard.admit(platform)
                if not admission.allowed:
                    account_id = str(account.get("id") or "")
                    clear_account_current_task(account_id, task_id)
                    refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                    retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, admission.retry_after))
                    mark_pending(task_id, "平台服务繁忙，任务已自动排队")
                    update_meta(task_id, next_attempt_at=retry_at.isoformat())
                    continue
                if not can_run_task(task_id, worker_id):
                    account_id = str(account.get("id") or "")
                    clear_account_current_task(account_id, task_id)
                    refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                    continue
                if platform == "doubao":
                    runner = DoubaoVideoAutomation(task_id, str(meta.get("prompt") or ""), str(meta.get("ratio") or "9:16"), str(meta.get("model") or "Seedance 2.0 Mini"), account=account)
                elif platform == "qianwen":
                    runner = QianwenVideoAutomation(task_id, str(meta.get("prompt") or ""), str(meta.get("ratio") or "9:16"), str(meta.get("model") or "万相 2.7"), str(meta.get("task_type") or "video"), account=account)
                else:
                    runner = DolaFetchAutomation(task_id, str(meta.get("prompt") or ""), str(meta.get("ratio") or "9:16"), account=account)
                    async with self._dola_submit_lock:
                        delay = DOLA_SUBMIT_INTERVAL_SECONDS - (asyncio.get_running_loop().time() - self._last_dola_submit_at)
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._last_dola_submit_at = asyncio.get_running_loop().time()
                outcome = await runner.run()
                if outcome.get("success"):
                    self._platform_guard.record_success(platform)
                elif outcome.get("retryable") and not outcome.get("account_fault"):
                    self._platform_guard.record_failure(platform)
                if outcome.get("success") and platform in {"dola", "doubao", "qianwen"} and account:
                    settle_account_quota(str(account.get("id") or ""), str(account.get("quota_charge_id") or ""))
                    clear_account_current_task(str(account.get("id") or ""), task_id)
                if not outcome.get("success"):
                    retry_count = 0
                    if outcome.get("submitted"):
                        if account:
                            settle_account_quota(str(account.get("id") or ""), str(account.get("quota_charge_id") or ""))
                            clear_account_current_task(str(account.get("id") or ""), task_id)
                        mark_submitted(task_id)
                        await asyncio.sleep(20)
                        continue
                    if account:
                        clear_account_current_task(str(account.get("id") or ""), task_id)
                        if outcome.get("retryable"):
                            if outcome.get("account_fault"):
                                record_failed_account(task_id, str(account.get("id") or ""))
                            consume_failed_account_quota(task_id, account, platform)
                    if outcome.get("retryable"):
                        reason = str(outcome.get("reason") or "")[:500]
                        retry_count = record_retry(task_id, reason)
                        if retry_count > MAX_TASK_RETRIES:
                            meta = get_meta(task_id)
                            refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                    else:
                        reason = str(outcome.get("reason") or "")[:500]
                        mark_failed(task_id, reason)
                        meta = get_meta(task_id)
                        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                        if account:
                            consume_failed_account_quota(task_id, account, platform)
                    await asyncio.sleep(2)
            except FileNotFoundError:
                pass
            except asyncio.CancelledError:
                if account:
                    account_id = str(account.get("id") or "")
                    clear_account_current_task(account_id, task_id)
                    with suppress(FileNotFoundError):
                        meta = get_meta(task_id)
                        if str(meta.get("status") or "") == "canceled":
                            refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                        elif not self._stopping:
                            mark_pending(task_id, "worker canceled")
                            refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                raise
            except Exception as exc:
                if "platform" in locals():
                    self._platform_guard.record_failure(platform)
                with suppress(FileNotFoundError):
                    retry_count = record_retry(task_id, str(exc)[:500])
                    if retry_count > MAX_TASK_RETRIES:
                        meta = get_meta(task_id)
                        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                if account:
                    clear_account_current_task(str(account.get("id") or ""), task_id)
                    if platform == "dola" or not is_final_generation_failure(str(exc)):
                        record_failed_account(task_id, str(account.get("id") or ""))
                        consume_failed_account_quota(task_id, account, platform)
                await asyncio.sleep(2)
            finally:
                self._queue.release(task_id, worker_id)
                self._claimed.discard(task_id)
                self._worker_task_ids.pop(worker_id, None)
                set_active_tasks(self._claimed)
                settings = load_settings()
                if settings.reclaim_memory_after_task:
                    queue_idle = not self._claimed and not has_pending_tasks(self._claimed)
                    await reclaim_memory_after_task(
                        idle=queue_idle,
                        drop_os_cache=settings.drop_os_cache_when_idle,
                    )


manager = WorkerManager()
