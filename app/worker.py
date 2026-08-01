from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone

from .accounts import account_supports_duration, claim_account_for_worker, clear_account_current_task, disable_account_for_login, exhaust_account_quota, local_today, mark_account_slider_verification, refund_account_quota, reset_daily_account_quotas_if_needed, settle_account_quota
from .api_proxy_pool import ReusableApiProxyPool
from .automation import DolaFetchAutomation, ReferenceUploadCapacityError, is_final_generation_failure, is_infrastructure_failure
from .browser_runtime import BROWSER_CONTEXTS_PER_PROCESS, BROWSER_POOL_PROCESSES, ReusableBrowserPool
from .doubao_automation import DoubaoVideoAutomation
from .qianwen_automation import QianwenVideoAutomation
from .proxy_manager import shutdown_task_mihomo_pool, task_mihomo_pool_snapshot
from .config import account_quota_cost_units, load_settings
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
    mark_late_result_success,
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
    task_retry_limit,
    update_meta,
    utc_now,
)
from .query import query_task
from .resilience import PlatformGuard, adaptive_worker_limit, fair_owner_capacity_limits
from .task_queue import get_task_queue, queue_backend
from .temp_access import refund_temp_quota_hash
from .temp_access import temp_token_concurrency_limits, temp_token_remote_generation_limits


GENERATING_TEXT = "正在为您生成视频，请稍候...本次使用 Seedance 2.0生成，预计等待 3~8 分钟。"
RUNNING_WATCH_GRACE_SECONDS = 90
RESULT_WATCH_DEADLINE_MINUTES = 20
DOUBAO_RESULT_WATCH_DEADLINE_MINUTES = 30
RESULT_LONG_WAIT_SECONDS = 8 * 60
RESULT_LOW_RATE_SECONDS = 15 * 60
RESULT_MAX_TOTAL_WATCH_SECONDS = 30 * 60
RESULT_LATE_POLL_INTERVAL_SECONDS = 60
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
IMAGE_PREPARATION_CONCURRENCY = _bounded_env_int("DOLA_IMAGE_PREPARE_CONCURRENCY", 8, 1, 24)
IMAGE_UPLOAD_SLOT_WAIT_SECONDS = _bounded_env_int("DOLA_IMAGE_UPLOAD_SLOT_WAIT_SECONDS", 20, 5, 120)
API_PROXY_REFRESH_CONCURRENCY = _bounded_env_int("DOLA_API_PROXY_REFRESH_CONCURRENCY", 2, 1, 4)


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
    if outcome.get("switch_account"):
        record_failed_account(task_id, account_id)
        update_meta(task_id, preferred_account_id="")
    if outcome.get("account_login_invalid"):
        platform_label = {"dola": "Dola", "doubao": "豆包", "qianwen": "千问"}.get(platform, platform)
        disable_account_for_login(account_id, f"{platform_label} 登录状态失效")
        refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
        return
    if outcome.get("account_slider_verification"):
        mark_account_slider_verification(account_id)
        refund_account_quota_once(task_id, account_id, str(account.get("quota_charge_id") or ""))
        return
    if outcome.get("account_quota_insufficient"):
        exhaust_account_quota(account_id, str(account.get("quota_charge_id") or ""))
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
        self._dola_submit_lock = asyncio.Lock()
        self._last_dola_submit_at = 0.0
        self._doubao_submit_lock = asyncio.Lock()
        self._last_doubao_submit_at = 0.0
        self._image_submission_semaphore = asyncio.Semaphore(IMAGE_SUBMISSION_CONCURRENCY)
        self._image_submission_condition = asyncio.Condition()
        self._image_submission_active = 0
        self._image_submission_reservations: dict[str, str] = {}
        self._claimed_image_preparations: dict[str, str] = {}
        self._image_prepare_owner_limits: dict[str, int] = {}
        self._image_prepare_owner_limits_refreshed_at = 0.0
        self._image_owner_limits: dict[str, int] = {}
        self._image_owner_limits_refreshed_at = 0.0
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
            contexts_per_endpoint=1,
            max_concurrent_refreshes=API_PROXY_REFRESH_CONCURRENCY,
        )
        self._remote_generation_reservations: dict[str, str] = {}

    async def start(self) -> None:
        if self._supervisor and not self._supervisor.done():
            return
        if queue_backend() != "file":
            self._queue.recover()
        reset_running_tasks()
        self._platform_guard.record_success("dola")
        self._queue.reconcile()
        self._requeue_stale_dola_guard_tasks()
        self._requeue_stale_resource_wait_tasks()
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

    def _requeue_stale_resource_wait_tasks(self) -> None:
        for task_id, meta in list_task_metas_by_statuses({"pending"}, platform="dola", limit=2000):
            queue_category = str(meta.get("queue_category") or "")
            infrastructure_error = str(meta.get("infrastructure_error") or "")
            browser_allocation_timeout = "execution phase timed out: allocating_browser" in infrastructure_error
            if queue_category not in {"image_upload_limit", "image_prepare_limit"} and not browser_allocation_timeout:
                continue
            updates: dict[str, object] = {
                "next_attempt_at": utc_now(),
                "queue_reason": "等待重新提交",
                "queue_category": "",
                "status_reason": "系统资源已恢复，等待重新提交",
                "execution_phase": "retry_queued",
                "phase_updated_at": utc_now(),
                "retry_queue_verified_at": "",
            }
            if browser_allocation_timeout:
                updates.update(infrastructure_retry_count=0, infrastructure_error="")
            update_meta(task_id, **updates)
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
        self._claimed_image_preparations.clear()
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
            "image_preparation_claimed": len(self._claimed_image_preparations),
            "image_preparation_claim_limit": IMAGE_PREPARATION_CONCURRENCY,
            # Retain the old fields for deployment dashboards during the rename.
            "image_submission_claimed": len(self._claimed_image_preparations),
            "image_submission_claim_limit": IMAGE_PREPARATION_CONCURRENCY,
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
        dola_submitted_rows = list_task_metas_by_statuses(
            {STATUS_SUBMITTED},
            platform="dola",
            due_before=due_before,
            limit=RESULT_POLL_BATCH_SIZE,
        )
        doubao_submitted_rows = list_task_metas_by_statuses(
            {STATUS_SUBMITTED},
            platform="doubao",
            due_before=due_before,
            limit=RESULT_POLL_BATCH_SIZE,
        )
        dola_late_rows = list_task_metas_by_statuses(
            {"failed"},
            platform="dola",
            due_before=due_before,
            limit=RESULT_POLL_BATCH_SIZE,
            execution_phase="late_result_watch",
        )
        doubao_late_rows = list_task_metas_by_statuses(
            {"failed"},
            platform="doubao",
            due_before=due_before,
            limit=RESULT_POLL_BATCH_SIZE,
            execution_phase="late_result_watch",
        )
        late_ids = []
        for task_id, meta in dola_late_rows + doubao_late_rows:
            if self._late_result_watch_active(meta):
                late_ids.append(task_id)
            else:
                update_meta(
                    task_id,
                    late_result_watch_until="",
                    execution_phase="failed",
                    status_reason=str(meta.get("error") or "generation timeout"),
                )
        await self._watch_unfinished_success_tasks(
            [task_id for task_id, _ in dola_submitted_rows + doubao_submitted_rows] + late_ids
        )
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
                    settings = load_settings()
                    phase_timeout = max(240, int(settings.task_timeout_seconds))
                    if str(meta.get("platform") or "") == "doubao" and str(meta.get("execution_phase") or "") == "submitting_request":
                        doubao_submit_timeout = 120 * (max(0, min(10, int(settings.doubao_submit_retry_limit))) + 1) + 90
                        phase_timeout = max(phase_timeout, doubao_submit_timeout)
                    stale_after = phase_timeout + RUNNING_WATCH_GRACE_SECONDS
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
                    if self._late_result_watch_active(get_meta(task_id)):
                        mark_late_result_success(task_id)
                    return
                meta = get_meta(task_id)
                platform = str(meta.get("platform") or "dola")
                if platform not in {"dola", "doubao"}:
                    return
                late_watch = self._late_result_watch_active(meta)
                if str(meta.get("status") or "") != STATUS_SUBMITTED and not late_watch:
                    return
                submitted_at = self._parse_utc(str(meta.get("submitted_at") or meta.get("updated_at") or ""))
                now = datetime.now(timezone.utc)
                if late_watch:
                    late_until = self._late_result_watch_deadline(meta)
                    if not late_until or now >= late_until:
                        update_meta(task_id, late_result_watch_until="", execution_phase="failed", status_reason=str(meta.get("error") or "generation timeout"))
                        return
                deadline_minutes = DOUBAO_RESULT_WATCH_DEADLINE_MINUTES if platform == "doubao" else RESULT_WATCH_DEADLINE_MINUTES
                if not late_watch and submitted_at and now - submitted_at >= timedelta(minutes=deadline_minutes):
                    account_id = str(result.get("account_id") or "")
                    if account_id:
                        if platform == "doubao":
                            refund_account_quota_once(task_id, account_id, str(result.get("account_quota_charge_id") or ""))
                        else:
                            settle_account_quota(account_id, str(result.get("account_quota_charge_id") or ""))
                        clear_account_current_task(account_id, task_id)
                    timeout_reason = "生成超过30分钟，仍未返回结果" if platform == "doubao" else "生成超过20分钟，仍未返回结果"
                    mark_failed(task_id, timeout_reason)
                    refund_temp_quota_once(task_id, str(meta.get("owner_token_hash") or ""))
                    late_until = submitted_at + timedelta(seconds=RESULT_MAX_TOTAL_WATCH_SECONDS)
                    if late_until > now:
                        update_meta(
                            task_id,
                            late_result_watch_until=late_until.isoformat(),
                            late_result_refunded_at=utc_now(),
                            execution_phase="late_result_watch",
                            status_reason=f"已退款，后台继续观察{'豆包' if platform == 'doubao' else 'Dola'}结果",
                            next_result_poll_at=(now + timedelta(seconds=RESULT_LATE_POLL_INTERVAL_SECONDS)).isoformat(),
                        )
                    else:
                        update_meta(
                            task_id,
                            late_result_watch_until="",
                            late_result_refunded_at=utc_now(),
                            execution_phase="failed",
                            status_reason=timeout_reason,
                            next_result_poll_at="",
                        )
                    return
                age_seconds = max(0.0, (now - submitted_at).total_seconds()) if submitted_at else 0.0
                if age_seconds >= RESULT_LONG_WAIT_SECONDS and not meta.get("long_result_wait_marked_at"):
                    update_meta(task_id, long_result_wait_marked_at=utc_now(), execution_phase="waiting_result_long", status_reason=f"{'豆包' if platform == 'doubao' else 'Dola'}生成时间较长，继续查询结果")
                if platform == "dola" and age_seconds >= RESULT_LOW_RATE_SECONDS and not meta.get("late_account_released_at"):
                    account_id = str(result.get("account_id") or "")
                    if account_id:
                        clear_account_current_task(account_id, task_id)
                    update_meta(task_id, late_account_released_at=utc_now(), execution_phase="waiting_result_low_rate", status_reason="Dola生成较慢，已释放账号并继续低频观察")
                await self._pace_result_poll()
                outcome = await query_task(task_id, late_watch=late_watch, background_poll=True)
                if str(outcome.get("code") or "") == "2":
                    return
                current = get_meta(task_id)
                if str(current.get("status") or "") != STATUS_SUBMITTED and not late_watch:
                    return
                miss_count = record_result_watch_miss(task_id)
                jitter = secrets.randbelow(5001) / 1000
                if late_watch:
                    interval = RESULT_LATE_POLL_INTERVAL_SECONDS
                elif platform == "doubao":
                    interval = 15 if age_seconds < 5 * 60 else 30 if age_seconds < 15 * 60 else 60
                else:
                    interval = RESULT_LATE_POLL_INTERVAL_SECONDS if age_seconds >= RESULT_LOW_RATE_SECONDS else min(45, RESULT_POLL_BASE_INTERVAL_SECONDS + max(0, miss_count - 1) * 5)
                interval = max(interval, max(0, int(outcome.get("retry_after") or 0)))
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
                    if str(current.get("status") or "") == STATUS_SUBMITTED or self._late_result_watch_active(current):
                        update_meta(
                            task_id,
                            next_result_poll_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                        )
                return
            finally:
                self._result_poll_active = max(0, self._result_poll_active - 1)

    def _late_result_watch_active(self, meta: dict) -> bool:
        if str(meta.get("status") or "") != "failed":
            return False
        until = self._late_result_watch_deadline(meta)
        return bool(until and datetime.now(timezone.utc) < until)

    def _late_result_watch_deadline(self, meta: dict) -> datetime | None:
        stored_until = self._parse_utc(str(meta.get("late_result_watch_until") or ""))
        if not stored_until:
            return None
        submitted_at = self._parse_utc(str(meta.get("submitted_at") or ""))
        absolute_until = (
            submitted_at + timedelta(seconds=RESULT_MAX_TOTAL_WATCH_SECONDS)
            if submitted_at
            else None
        )
        if absolute_until:
            return min(stored_until, absolute_until)
        return stored_until

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
            configured = temp_token_concurrency_limits()
            try:
                contenders = {
                    str(meta.get("owner_token_hash") or "")
                    for _, meta in list_task_metas_by_statuses({"pending", "running"})
                    if str(meta.get("owner_token_hash") or "")
                }
                requested = {owner: configured[owner] for owner in contenders if owner in configured}
                self._token_concurrency_limits = (
                    fair_owner_capacity_limits(requested, self._dola_browser_pool.capacity)
                    if requested
                    else configured
                )
            except Exception as exc:
                self._last_error = str(exc)[:500]
                self._token_concurrency_limits = configured
            self._token_concurrency_refreshed_at = now
        return self._token_concurrency_limits

    def _image_owner_limit(self, owner: str) -> int:
        normalized_owner = str(owner or "")
        if not normalized_owner:
            return IMAGE_SUBMISSION_CONCURRENCY
        now = asyncio.get_running_loop().time()
        if now - self._image_owner_limits_refreshed_at >= 1.0 or normalized_owner not in self._image_owner_limits:
            contenders = {
                str(meta.get("owner_token_hash") or "")
                for _, meta in list_task_metas_by_statuses({"pending", "running"}, platform="dola")
                if int(meta.get("image_count") or 0) > 0 and str(meta.get("owner_token_hash") or "")
            }
            contenders.add(normalized_owner)
            configured = self._owner_concurrency_limits()
            requested = {
                contender: min(IMAGE_SUBMISSION_CONCURRENCY, max(1, int(configured.get(contender) or IMAGE_SUBMISSION_CONCURRENCY)))
                for contender in contenders
            }
            self._image_owner_limits = fair_owner_capacity_limits(requested, IMAGE_SUBMISSION_CONCURRENCY)
            self._image_owner_limits_refreshed_at = now
        return max(1, int(self._image_owner_limits.get(normalized_owner) or 1))

    def _image_prepare_owner_limit(self, owner: str) -> int:
        normalized_owner = str(owner or "")
        if not normalized_owner:
            return IMAGE_PREPARATION_CONCURRENCY
        now = asyncio.get_running_loop().time()
        if now - self._image_prepare_owner_limits_refreshed_at >= 1.0 or normalized_owner not in self._image_prepare_owner_limits:
            contenders = {
                str(meta.get("owner_token_hash") or "")
                for _, meta in list_task_metas_by_statuses({"pending", "running"}, platform="dola")
                if int(meta.get("image_count") or 0) > 0 and str(meta.get("owner_token_hash") or "")
            }
            contenders.add(normalized_owner)
            configured = self._owner_concurrency_limits()
            requested = {
                contender: min(
                    IMAGE_PREPARATION_CONCURRENCY,
                    max(1, int(configured.get(contender) or IMAGE_PREPARATION_CONCURRENCY)),
                )
                for contender in contenders
            }
            self._image_prepare_owner_limits = fair_owner_capacity_limits(requested, IMAGE_PREPARATION_CONCURRENCY)
            self._image_prepare_owner_limits_refreshed_at = now
        return max(1, int(self._image_prepare_owner_limits.get(normalized_owner) or 1))

    def _release_image_preparation(self, task_id: str) -> None:
        self._claimed_image_preparations.pop(str(task_id or ""), None)

    @asynccontextmanager
    async def _image_upload_slot(self, task_id: str, owner: str):
        normalized_owner = str(owner or "")
        reserved = False
        active = False
        deadline = asyncio.get_running_loop().time() + IMAGE_UPLOAD_SLOT_WAIT_SECONDS
        try:
            while not reserved:
                async with self._image_submission_condition:
                    owner_limit = self._image_owner_limit(normalized_owner)
                    owner_active = sum(
                        reserved_owner == normalized_owner
                        for reserved_owner in self._image_submission_reservations.values()
                    )
                    if (
                        len(self._image_submission_reservations) < IMAGE_SUBMISSION_CONCURRENCY
                        and owner_active < owner_limit
                    ):
                        self._image_submission_reservations[task_id] = normalized_owner
                        reserved = True
                        break
                    set_execution_phase(task_id, "waiting_image_upload_slot", "等待参考图上传时段")
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ReferenceUploadCapacityError()
                    try:
                        await asyncio.wait_for(self._image_submission_condition.wait(), timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise ReferenceUploadCapacityError() from exc
            async with self._image_submission_semaphore:
                self._image_submission_active += 1
                active = True
                yield
        finally:
            if active:
                self._image_submission_active = max(0, self._image_submission_active - 1)
            if reserved:
                async with self._image_submission_condition:
                    self._image_submission_reservations.pop(task_id, None)
                    self._image_submission_condition.notify_all()

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

    async def _wait_for_dola_submit_slot(self, _exit_id: str = "direct") -> None:
        # Dola rate limiting is global: changing the proxy IP must not allow
        # several generation submissions to bypass the configured interval.
        async with self._dola_submit_lock:
            submit_interval = load_settings().dola_global_submit_interval_seconds
            delay = submit_interval - (asyncio.get_running_loop().time() - self._last_dola_submit_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_dola_submit_at = asyncio.get_running_loop().time()

    async def _wait_for_doubao_submit_slot(self) -> None:
        # Releasing the browser after submission must not create a burst of
        # new Doubao generation requests.
        async with self._doubao_submit_lock:
            submit_interval = max(5.0, float(load_settings().dola_submit_interval_seconds))
            delay = submit_interval - (asyncio.get_running_loop().time() - self._last_doubao_submit_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_doubao_submit_at = asyncio.get_running_loop().time()

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
                    candidate_owner = str(claimed_meta.get("owner_token_hash") or "")
                    if is_image_submission:
                        prepare_owner_limit = self._image_prepare_owner_limit(candidate_owner)
                        prepare_owner_active = sum(
                            claimed_owner == candidate_owner
                            for claimed_owner in self._claimed_image_preparations.values()
                        )
                        if (
                            len(self._claimed_image_preparations) >= IMAGE_PREPARATION_CONCURRENCY
                            or prepare_owner_active >= prepare_owner_limit
                        ):
                            defer_task(candidate_id, "参考图任务等待页面准备通道", "image_prepare_limit", 10)
                            self._queue.release(candidate_id, worker_id)
                            continue
                    if candidate_platform == "dola" and not self._reserve_remote_generation_slot(candidate_id, claimed_meta):
                        defer_task(candidate_id, "当前用户远端生成任务已达上限，继续排队", "remote_limit", 5)
                        self._queue.release(candidate_id, worker_id)
                        continue
                    task_id = candidate_id
                    self._claimed.add(task_id)
                    if is_image_submission:
                        self._claimed_image_preparations[task_id] = candidate_owner
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
                preferred_account_id = str(meta.get("preferred_account_id") or "").strip().lower()
                duration = int(meta.get("duration") or 10)
                if preferred_account_id and not account_supports_duration(preferred_account_id, platform, duration):
                    update_meta(task_id, preferred_account_id="", queue_reason="", queue_category="")
                    preferred_account_id = ""
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
                    quota_cost=account_quota_cost_units(
                        platform,
                        str(meta.get("model") or ""),
                        duration,
                    ),
                    duration=duration,
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
                if platform == "qianwen":
                    set_execution_phase(task_id, "submitting_request", "正在提交生成请求")
                if platform == "doubao":
                    proxy_session = DolaFetchAutomation(
                        task_id,
                        "",
                        "",
                        account=account,
                        api_proxy_pool=self._api_proxy_pool,
                        proxy_platform="doubao",
                    )
                    runner = DoubaoVideoAutomation(
                        task_id,
                        str(meta.get("prompt") or ""),
                        str(meta.get("ratio") or "9:16"),
                        str(meta.get("model") or "Seedance 2.0 Mini"),
                        int(meta.get("duration") or 10),
                        account=account,
                        proxy_session=proxy_session,
                        browser_pool=self._dola_browser_pool,
                        submission_pacer=self._wait_for_doubao_submit_slot,
                    )
                elif platform == "qianwen":
                    proxy_session = DolaFetchAutomation(
                        task_id,
                        "",
                        "",
                        account=account,
                        api_proxy_pool=self._api_proxy_pool,
                        proxy_platform="qianwen",
                    )
                    runner = QianwenVideoAutomation(
                        task_id,
                        str(meta.get("prompt") or ""),
                        str(meta.get("ratio") or "9:16"),
                        str(meta.get("model") or "万相 2.7"),
                        str(meta.get("task_type") or "video"),
                        int(meta.get("duration") or 10),
                        account=account,
                        proxy_session=proxy_session,
                    )
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
                        image_upload_slot=(
                            lambda current_task_id=task_id, current_owner=candidate_owner: self._image_upload_slot(current_task_id, current_owner)
                        ) if is_image_submission else None,
                        image_preparation_done=(
                            lambda current_task_id=task_id: self._release_image_preparation(current_task_id)
                        ) if is_image_submission else None,
                    )
                outcome = await runner.run()
                if platform != "dola":
                    if outcome.get("success"):
                        self._platform_guard.record_success(platform)
                    elif outcome.get("retryable") and not outcome.get("account_fault") and not outcome.get("infrastructure_fault"):
                        self._platform_guard.record_failure(platform)
                if outcome.get("success") and platform in {"dola", "doubao", "qianwen"} and account:
                    if not outcome.get("confirmation_pending"):
                        settle_account_quota(str(account.get("id") or ""), str(account.get("quota_charge_id") or ""))
                    if not outcome.get("keep_account_claimed"):
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
                self._release_image_preparation(task_id)
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
