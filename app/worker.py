from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from .accounts import claim_account_for_worker, clear_account_current_task, local_today, mark_account_slider_verification, refund_account_quota, reset_daily_account_quotas_if_needed, settle_account_quota
from .api_proxy_pool import ReusableApiProxyPool
from .automation import DolaFetchAutomation, is_final_generation_failure, is_infrastructure_failure
from .browser_runtime import BROWSER_CONTEXTS_PER_PROCESS, BROWSER_POOL_PROCESSES, ReusableBrowserPool
from .doubao_automation import DoubaoVideoAutomation
from .qianwen_automation import QianwenVideoAutomation
from .proxy_manager import shutdown_task_mihomo_pool, task_mihomo_pool_snapshot
from .config import load_settings
from .memory import reclaim_memory_after_task
from .store import (
    claim_next_pending,
    count_pending_tasks,
    can_run_task,
    defer_task,
    get_meta,
    has_pending_tasks,
    expire_task_if_timeout,
    is_task_canceled,
    list_task_metas_by_statuses,
    load_result,
    mark_account_refund_once,
    mark_failed,
    mark_pending,
    mark_retry_queue_verified,
    mark_submitted,
    mark_result_once,
    MAX_INFRASTRUCTURE_RETRIES,
    record_failed_account,
    record_result_watch_miss,
    record_retry,
    record_infrastructure_retry,
    reset_running_tasks,
    set_execution_phase,
    set_active_tasks,
    STATUS_SUBMITTED,
    task_image_paths,
    task_retry_limit,
    update_meta,
    utc_now,
)
from .query import query_task
from .resilience import PlatformGuard, adaptive_worker_limit
from .task_queue import get_task_queue, queue_backend
from .temp_access import refund_temp_quota_hash
from .temp_access import temp_token_concurrency_limits, temp_token_remote_generation_limits


GENERATING_TEXT = "正在为您生成视频，请稍候...本次使用 Seedance 2.0生成，预计等待 3~8 分钟。"
RUNNING_WATCH_GRACE_SECONDS = 90
RESULT_WATCH_DEADLINE_MINUTES = 20
RETRY_ACCOUNT_WAIT_MINUTES = 5


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name) or default)))
    except (TypeError, ValueError):
        return default


RESULT_POLL_CONCURRENCY = _bounded_env_int("DOLA_RESULT_POLL_CONCURRENCY", 16, 1, 64)
RESULT_POLL_RATE_PER_SECOND = _bounded_env_int("DOLA_RESULT_POLL_RATE_PER_SECOND", 8, 1, 32)
RESULT_POLL_BATCH_SIZE = _bounded_env_int("DOLA_RESULT_POLL_BATCH_SIZE", 256, RESULT_POLL_CONCURRENCY, 2000)
RESULT_POLL_BASE_INTERVAL_SECONDS = _bounded_env_int("DOLA_RESULT_POLL_INTERVAL_SECONDS", 20, 10, 120)
RESULT_WATCH_INTERVAL_SECONDS = _bounded_env_int("DOLA_RESULT_WATCH_INTERVAL_SECONDS", 5, 2, 60)
IMAGE_SUBMISSION_CONCURRENCY = _bounded_env_int("DOLA_IMAGE_UPLOAD_CONCURRENCY", 8, 1, 16)


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


def should_consume_retry_account_quota(outcome: dict) -> bool:
    return bool(outcome.get("retryable")) and not bool(outcome.get("infrastructure_fault"))


def defer_non_counting_retry(task_id: str, outcome: dict) -> bool:
    if not bool(outcome.get("defer_only")):
        return False
    defer_task(
        task_id,
        str(outcome.get("defer_reason") or "生成节点冷却中，任务已自动排队"),
        str(outcome.get("defer_category") or "proxy_cooldown"),
        max(1, int(outcome.get("retry_after") or 5)),
    )
    return True


def release_account_after_retryable_failure(task_id: str, account: dict, platform: str, outcome: dict) -> None:
    account_id = str(account.get("id") or "")
    if not account_id:
        return
    clear_account_current_task(account_id, task_id)
    if outcome.get("account_slider_verification"):
        mark_account_slider_verification(account_id)
        refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
        return
    if outcome.get("infrastructure_fault"):
        refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
    elif outcome.get("account_fault"):
        record_failed_account(task_id, account_id)
    if should_consume_retry_account_quota(outcome):
        consume_failed_account_quota(task_id, account, platform)


class WorkerManager:
    def __init__(self) -> None:
        self._supervisor: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._workers: dict[str, asyncio.Task] = {}
        self._worker_task_ids: dict[str, str] = {}
        self._claim_lock = asyncio.Lock()
        self._dola_submit_locks: dict[str, asyncio.Lock] = {}
        self._last_dola_submit_at: dict[str, float] = {}
        self._image_submission_semaphore = asyncio.Semaphore(IMAGE_SUBMISSION_CONCURRENCY)
        self._image_submission_active = 0
        self._image_submission_reservations: set[str] = set()
        self._result_poll_semaphore = asyncio.Semaphore(RESULT_POLL_CONCURRENCY)
        self._result_poll_pace_lock = asyncio.Lock()
        self._last_result_poll_at = 0.0
        self._result_poll_active = 0
        self._token_concurrency_limits: dict[str, int] = {}
        self._token_concurrency_refreshed_at = 0.0
        self._remote_owner_counts: dict[str, int] = {}
        self._remote_owner_limits: dict[str, int] = {}
        self._remote_owner_refreshed_at = 0.0
        self._last_pending_retry_reconcile_at = 0.0
        self._account_maintenance_date = ""
        self._claimed: set[str] = set()
        self._stopping = False
        self._worker_seq = 0
        self._instance_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
        self._restart_count = 0
        self._last_error = ""
        self._queue = get_task_queue()
        self._platform_guard = PlatformGuard(getattr(self._queue, "client", None))
        self._resource_snapshot: dict[str, object] = {}
        self._dola_browser_pool = ReusableBrowserPool(
            max_processes=BROWSER_POOL_PROCESSES,
            contexts_per_process=BROWSER_CONTEXTS_PER_PROCESS,
        )
        self._api_proxy_pool = ReusableApiProxyPool(
            max_endpoints=BROWSER_POOL_PROCESSES,
            contexts_per_endpoint=BROWSER_CONTEXTS_PER_PROCESS,
        )
        self._remote_generation_reservations: dict[str, str] = {}

    async def start(self) -> None:
        if self._supervisor and not self._supervisor.done():
            return
        if queue_backend() == "file":
            reset_running_tasks()
        else:
            self._queue.recover()
        self._platform_guard.record_success("dola")
        self._queue.reconcile()
        self._requeue_stale_dola_guard_tasks()
        self._stopping = False
        await self._dola_browser_pool.start()
        self._supervisor = asyncio.create_task(self._supervise())
        self._watchdog = asyncio.create_task(self._watch_running_tasks())

    def _requeue_stale_dola_guard_tasks(self) -> None:
        for task_id, meta in list_task_metas_by_statuses({"pending"}, platform="dola", limit=2000):
            if str(meta.get("queue_category") or "") != "platform_guard":
                continue
            update_meta(
                task_id,
                next_attempt_at=utc_now(),
                queue_reason="等待重新提交",
                queue_category="",
                status_reason="等待重新提交",
            )
            self._queue.requeue(task_id)

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
        self._remote_generation_reservations.clear()
        self._image_submission_reservations.clear()
        self._worker_task_ids.clear()
        set_active_tasks([])
        await self._dola_browser_pool.stop()
        await self._api_proxy_pool.stop()
        await shutdown_task_mihomo_pool()

    def health_snapshot(self) -> dict:
        configured = self._dola_browser_pool.capacity
        effective, resource = adaptive_worker_limit(configured, self._dola_browser_pool.capacity)
        resource = {**resource, "effective_workers": effective, "browser_pool_capacity": self._dola_browser_pool.capacity}
        supervisor_alive = bool(self._supervisor and not self._supervisor.done())
        watchdog_alive = bool(self._watchdog and not self._watchdog.done())
        worker_alive = sum(1 for task in self._workers.values() if not task.done())
        healthy = supervisor_alive and watchdog_alive and worker_alive >= 1
        remote_generating = self._remote_generation_count()
        return {
            "ok": healthy,
            "supervisor_alive": supervisor_alive,
            "watchdog_alive": watchdog_alive,
            "worker_alive": worker_alive,
            "worker_configured": configured,
            "worker_effective": effective,
            "claimed": len(self._claimed),
            "result_poll_active": self._result_poll_active,
            "result_poll_concurrency": RESULT_POLL_CONCURRENCY,
            "result_poll_rate_per_second": RESULT_POLL_RATE_PER_SECOND,
            "image_upload_active": self._image_submission_active,
            "image_upload_concurrency": IMAGE_SUBMISSION_CONCURRENCY,
            "image_upload_reserved": len(self._image_submission_reservations),
            "browser_pool": self._dola_browser_pool.snapshot(),
            "api_proxy_pool": self._api_proxy_pool.snapshot(),
            "proxy_exit_pool": task_mihomo_pool_snapshot(),
            "remote_generation_limit": 0,
            "remote_generation_active": remote_generating,
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

    def _idle_workers_to_trim(self, desired: int) -> list[str]:
        excess = max(0, len(self._workers) - max(1, int(desired)))
        if not excess:
            return []
        idle_ids = [worker_id for worker_id in self._workers if worker_id not in self._worker_task_ids]
        return idle_ids[:excess]

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
                account_today = local_today()
                if self._account_maintenance_date != account_today:
                    reset_daily_account_quotas_if_needed()
                    self._account_maintenance_date = account_today
                settings = load_settings()
                configured = self._dola_browser_pool.capacity
                effective, self._resource_snapshot = adaptive_worker_limit(configured, self._dola_browser_pool.capacity)
                self._resource_snapshot = {**self._resource_snapshot, "effective_workers": effective, "browser_pool_capacity": self._dola_browser_pool.capacity}
                self._queue.heartbeat({task_id: worker_id for worker_id, task_id in self._worker_task_ids.items()})
                demand = len(self._claimed)
                with suppress(Exception):
                    demand += count_pending_tasks()
                desired = min(effective, max(1, demand))
                for worker_id in self._idle_workers_to_trim(desired):
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
            await asyncio.sleep(RESULT_WATCH_INTERVAL_SECONDS)
            try:
                await self._watch_running_tasks_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)[:500]
                self._restart_count += 1

    async def _watch_running_tasks_once(self) -> None:
        due_before = datetime.now(timezone.utc).isoformat()
        submitted_rows = list_task_metas_by_statuses(
            {STATUS_SUBMITTED},
            platform="dola",
            due_before=due_before,
            limit=RESULT_POLL_BATCH_SIZE,
        )
        await self._watch_unfinished_success_tasks([task_id for task_id, _ in submitted_rows])
        loop_now = asyncio.get_running_loop().time()
        if loop_now - self._last_pending_retry_reconcile_at >= 15:
            self._last_pending_retry_reconcile_at = loop_now
            await asyncio.to_thread(self._reconcile_pending_retries)
        running_ids = [task_id for task_id, _ in list_task_metas_by_statuses({"running"})]
        for task_id in running_ids:
            with suppress(FileNotFoundError):
                meta = get_meta(task_id)
                if str(meta.get("status") or "") != "running":
                    continue
                now = datetime.now(timezone.utc)
                started_at = self._parse_utc(str(meta.get("started_at") or meta.get("updated_at") or ""))
                phase_updated_at = self._parse_utc(str(meta.get("phase_updated_at") or "")) or started_at
                running_age = now - started_at if started_at else timedelta.max
                phase_age = now - phase_updated_at if phase_updated_at else timedelta.max
                if running_age < timedelta(seconds=RUNNING_WATCH_GRACE_SECONDS):
                    continue
                worker_id = str(meta.get("worker_id") or "")
                task = self._workers.get(worker_id) if worker_id else None
                if task and not task.done():
                    stale_after = max(240, int(load_settings().task_timeout_seconds)) + RUNNING_WATCH_GRACE_SECONDS
                    if phase_age < timedelta(seconds=stale_after):
                        continue
                    reason = f"execution phase timed out: {str(meta.get('execution_phase') or 'unknown')}"
                    retry_count = record_infrastructure_retry(task_id, reason)
                    if retry_count > MAX_INFRASTRUCTURE_RETRIES:
                        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                    task.cancel()
                    continue
                retry_count = record_infrastructure_retry(task_id, "worker execution heartbeat missing")
                self._claimed.discard(task_id)
                if worker_id:
                    if task and not task.done():
                        task.cancel()
                    self._worker_task_ids.pop(worker_id, None)
                if retry_count <= MAX_INFRASTRUCTURE_RETRIES:
                    self._queue.requeue(task_id)
                else:
                    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
        set_active_tasks(self._claimed)

    def _reconcile_pending_retries(self) -> None:
        now = datetime.now(timezone.utc)
        rows = list_task_metas_by_statuses({"pending"}, limit=RESULT_POLL_BATCH_SIZE)
        for task_id, meta in rows:
            retry_count = max(0, int(meta.get("retry_count") or 0))
            infrastructure_retry_count = max(0, int(meta.get("infrastructure_retry_count") or 0))
            if retry_count < 1 and infrastructure_retry_count < 1:
                continue
            if expire_task_if_timeout(task_id):
                continue
            next_attempt_at = self._parse_utc(str(meta.get("next_attempt_at") or ""))
            if next_attempt_at and next_attempt_at > now:
                continue
            try:
                if self._queue.requeue(task_id, next_attempt_at):
                    mark_retry_queue_verified(task_id)
            except Exception as exc:
                self._last_error = f"retry queue reconcile failed: {str(exc)[:450]}"

    async def _watch_unfinished_success_tasks(self, task_ids: list[str]) -> None:
        if not task_ids:
            return
        results = await asyncio.gather(
            *(self._watch_unfinished_success_task(task_id) for task_id in task_ids),
            return_exceptions=True,
        )
        errors = [str(result)[:500] for result in results if isinstance(result, Exception)]
        if errors:
            self._last_error = errors[-1]

    async def _pace_result_poll(self) -> None:
        interval = 1.0 / RESULT_POLL_RATE_PER_SECOND
        async with self._result_poll_pace_lock:
            now = asyncio.get_running_loop().time()
            delay = interval - (now - self._last_result_poll_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_result_poll_at = asyncio.get_running_loop().time()

    async def _watch_unfinished_success_task(self, task_id: str) -> None:
        async with self._result_poll_semaphore:
            self._result_poll_active += 1
            try:
                result = load_result(task_id)
                if result.get("decoded_main_url"):
                    return
                meta = get_meta(task_id)
                if str(meta.get("status") or "") != STATUS_SUBMITTED or str(meta.get("platform") or "dola") != "dola":
                    return
                submitted_at = self._parse_utc(str(meta.get("submitted_at") or meta.get("updated_at") or ""))
                if submitted_at and datetime.now(timezone.utc) - submitted_at >= timedelta(minutes=RESULT_WATCH_DEADLINE_MINUTES):
                    account_id = str(result.get("account_id") or "")
                    if account_id:
                        settle_account_quota(account_id, str(result.get("account_quota_charge_id") or ""))
                        clear_account_current_task(account_id, task_id)
                    mark_failed(task_id, "生成超过20分钟，仍未返回结果")
                    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                    return
                await self._pace_result_poll()
                outcome = await query_task(task_id)
                if str(outcome.get("code") or "") == "2":
                    return
                current = get_meta(task_id)
                if str(current.get("status") or "") != STATUS_SUBMITTED:
                    return
                miss_count = record_result_watch_miss(task_id)
                jitter = secrets.randbelow(5001) / 1000
                interval = min(45, RESULT_POLL_BASE_INTERVAL_SECONDS + max(0, miss_count - 1) * 5)
                update_meta(
                    task_id,
                    next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=interval + jitter)).isoformat(),
                )
            except FileNotFoundError:
                return
            except Exception as exc:
                self._last_error = str(exc)[:500]
                with suppress(Exception):
                    current = get_meta(task_id)
                    if str(current.get("status") or "") == STATUS_SUBMITTED:
                        update_meta(
                            task_id,
                            next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                        )
                return
            finally:
                self._result_poll_active = max(0, self._result_poll_active - 1)

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
        defer_task(task_id, f"等待可用{platform}账号", "account_wait", 5 + secrets.randbelow(6))
        return False

    def _owner_concurrency_limits(self) -> dict[str, int]:
        now = asyncio.get_running_loop().time()
        if now - self._token_concurrency_refreshed_at >= 1.0:
            self._token_concurrency_limits = temp_token_concurrency_limits()
            self._token_concurrency_refreshed_at = now
        return self._token_concurrency_limits

    def _remote_generation_count(self, fail_closed: bool = False) -> int:
        try:
            return len(list_task_metas_by_statuses({STATUS_SUBMITTED}, platform="dola"))
        except Exception:
            return 0

    def _remote_generation_owner_state(self) -> tuple[dict[str, int], dict[str, int]]:
        now = asyncio.get_running_loop().time()
        if now - self._remote_owner_refreshed_at >= 1.0:
            try:
                counts: dict[str, int] = {}
                for _, meta in list_task_metas_by_statuses({STATUS_SUBMITTED}, platform="dola"):
                    owner = str(meta.get("owner_token_hash") or "")
                    if owner:
                        counts[owner] = counts.get(owner, 0) + 1
                self._remote_owner_counts = counts
                self._remote_owner_limits = temp_token_remote_generation_limits()
                self._remote_owner_refreshed_at = now
            except Exception as exc:
                self._last_error = str(exc)[:500]
        return self._remote_owner_counts, self._remote_owner_limits

    def _reserve_remote_generation_slot(self, task_id: str, meta: dict) -> bool:
        owner = str(meta.get("owner_token_hash") or "")
        if not owner:
            self._remote_generation_reservations[task_id] = ""
            return True
        counts, limits = self._remote_generation_owner_state()
        limit = max(1, min(999, int(limits.get(owner) or 1)))
        reserved = sum(reserved_owner == owner for reserved_owner in self._remote_generation_reservations.values())
        if counts.get(owner, 0) + reserved >= limit:
            return False
        self._remote_generation_reservations[task_id] = owner
        return True

    async def _wait_for_dola_submit_slot(self, exit_id: str = "direct") -> None:
        normalized_exit = str(exit_id or "direct").strip()[:160] or "direct"
        lock = self._dola_submit_locks.setdefault(normalized_exit, asyncio.Lock())
        async with lock:
            submit_interval = load_settings().dola_exit_submit_interval_seconds
            last_submit_at = self._last_dola_submit_at.get(normalized_exit, 0.0)
            delay = submit_interval - (asyncio.get_running_loop().time() - last_submit_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_dola_submit_at[normalized_exit] = asyncio.get_running_loop().time()

    async def _worker_loop(self, worker_id: str) -> None:
        while not self._stopping:
            async with self._claim_lock:
                active_counts: dict[str, int] = {}
                for claimed_id in self._claimed:
                    with suppress(FileNotFoundError):
                        owner = str(get_meta(claimed_id).get("owner_token_hash") or "")
                        if owner:
                            active_counts[owner] = active_counts.get(owner, 0) + 1
                task_id = None
                for _ in range(20):
                    candidate_id = self._queue.claim(worker_id, self._claimed, active_counts, self._owner_concurrency_limits())
                    if not candidate_id:
                        break
                    claimed_meta = get_meta(candidate_id)
                    candidate_platform = str(claimed_meta.get("platform") or "dola")
                    is_image_submission = candidate_platform == "dola" and int(claimed_meta.get("image_count") or 0) > 0
                    if is_image_submission and len(self._image_submission_reservations) >= IMAGE_SUBMISSION_CONCURRENCY:
                        defer_task(candidate_id, "等待参考图上传资源", "image_upload_limit", 2)
                        self._queue.release(candidate_id, worker_id)
                        continue
                    if candidate_platform == "dola" and not self._reserve_remote_generation_slot(candidate_id, claimed_meta):
                        defer_task(candidate_id, "当前用户远端生成任务已达上限，继续排队", "remote_limit", 5)
                        self._queue.release(candidate_id, worker_id)
                        continue
                    if is_image_submission:
                        self._image_submission_reservations.add(candidate_id)
                    task_id = candidate_id
                    self._claimed.add(task_id)
                    self._worker_task_ids[worker_id] = task_id
                    break
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
                preferred_account_id = str(meta.get("preferred_account_id") or "").strip().lower() if platform == "dola" else ""
                if platform not in {"dola", "doubao", "qianwen"}:
                    mark_failed(task_id, "该平台网页自动化暂未接入")
                    continue
                if not can_run_task(task_id, worker_id):
                    continue
                account = claim_account_for_worker(
                    worker_id,
                    task_id,
                    exclude_ids=failed_account_ids,
                    platform=platform,
                    preferred_id=preferred_account_id,
                )
                if not account:
                    if preferred_account_id:
                        defer_task(task_id, "等待原账号更换代理重试", "preferred_account", 3)
                        continue
                    if not self._handle_unavailable_account(task_id, meta, platform):
                        await asyncio.sleep(3)
                    continue
                if meta.get("retry_queued_at"):
                    update_meta(task_id, retry_queued_at="")
                if platform != "dola":
                    admission = self._platform_guard.admit(platform)
                    if not admission.allowed:
                        account_id = str(account.get("id") or "")
                        clear_account_current_task(account_id, task_id)
                        refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                        defer_task(task_id, "平台服务繁忙，任务已自动排队", "platform_guard", max(1, admission.retry_after))
                        continue
                if not can_run_task(task_id, worker_id):
                    account_id = str(account.get("id") or "")
                    clear_account_current_task(account_id, task_id)
                    refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
                    continue
                if platform != "dola":
                    set_execution_phase(task_id, "submitting_request", "正在提交生成请求")
                if platform == "doubao":
                    runner = DoubaoVideoAutomation(task_id, str(meta.get("prompt") or ""), str(meta.get("ratio") or "9:16"), str(meta.get("model") or "Seedance 2.0 Mini"), account=account)
                elif platform == "qianwen":
                    runner = QianwenVideoAutomation(task_id, str(meta.get("prompt") or ""), str(meta.get("ratio") or "9:16"), str(meta.get("model") or "万相 2.7"), str(meta.get("task_type") or "video"), account=account)
                else:
                    runner = DolaFetchAutomation(
                        task_id,
                        str(meta.get("prompt") or ""),
                        str(meta.get("ratio") or "9:16"),
                        int(meta.get("duration") or 0),
                        account=account,
                        browser_pool=self._dola_browser_pool,
                        api_proxy_pool=self._api_proxy_pool,
                        submission_pacer=self._wait_for_dola_submit_slot,
                    )
                if platform == "dola" and task_image_paths(task_id):
                    set_execution_phase(task_id, "waiting_image_upload_slot", "等待参考图上传时段")
                    async with self._image_submission_semaphore:
                        self._image_submission_active += 1
                        try:
                            outcome = await runner.run()
                        finally:
                            self._image_submission_active = max(0, self._image_submission_active - 1)
                else:
                    outcome = await runner.run()
                if platform != "dola":
                    if outcome.get("success"):
                        self._platform_guard.record_success(platform)
                    elif outcome.get("retryable") and not outcome.get("account_fault") and not outcome.get("infrastructure_fault"):
                        self._platform_guard.record_failure(platform)
                if outcome.get("success") and platform in {"dola", "doubao", "qianwen"} and account:
                    if not outcome.get("confirmation_pending"):
                        settle_account_quota(str(account.get("id") or ""), str(account.get("quota_charge_id") or ""))
                    clear_account_current_task(str(account.get("id") or ""), task_id)
                if not outcome.get("success"):
                    retry_count = 0
                    if outcome.get("submitted"):
                        if account:
                            clear_account_current_task(str(account.get("id") or ""), task_id)
                        mark_submitted(task_id, result_poll_delay_seconds=15)
                        await asyncio.sleep(20)
                        continue
                    if account:
                        if outcome.get("retryable"):
                            release_account_after_retryable_failure(task_id, account, platform, outcome)
                        else:
                            clear_account_current_task(str(account.get("id") or ""), task_id)
                    if outcome.get("retryable"):
                        reason = str(outcome.get("reason") or "")[:500]
                        deferred = defer_non_counting_retry(task_id, outcome)
                        if not deferred:
                            if outcome.get("infrastructure_fault"):
                                retry_count = record_infrastructure_retry(task_id, reason)
                                retry_limit = MAX_INFRASTRUCTURE_RETRIES
                            else:
                                retry_count = record_retry(task_id, reason)
                                retry_limit = task_retry_limit()
                            if retry_count > retry_limit:
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
                reason = str(exc)[:500]
                infrastructure_fault = is_infrastructure_failure(reason)
                if "platform" in locals() and platform != "dola":
                    if not infrastructure_fault:
                        self._platform_guard.record_failure(platform)
                with suppress(FileNotFoundError):
                    retry_count = record_infrastructure_retry(task_id, reason) if infrastructure_fault else record_retry(task_id, reason)
                    retry_limit = MAX_INFRASTRUCTURE_RETRIES if infrastructure_fault else task_retry_limit()
                    if retry_count > retry_limit:
                        meta = get_meta(task_id)
                        refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                if account:
                    clear_account_current_task(str(account.get("id") or ""), task_id)
                    if infrastructure_fault:
                        refund_account_quota_once(task_id, str(account.get("id") or ""), str(account.get("quota_charge_id") or ""))
                    elif platform == "dola" or not is_final_generation_failure(reason):
                        record_failed_account(task_id, str(account.get("id") or ""))
                        consume_failed_account_quota(task_id, account, platform)
                await asyncio.sleep(2)
            finally:
                self._queue.release(task_id, worker_id)
                self._claimed.discard(task_id)
                self._image_submission_reservations.discard(task_id)
                reserved_owner = self._remote_generation_reservations.pop(task_id, None)
                if reserved_owner:
                    self._remote_owner_refreshed_at = 0.0
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
