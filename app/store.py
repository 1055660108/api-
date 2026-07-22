from __future__ import annotations

import json
import hashlib
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import TASKS_DIR, ensure_dirs
from .platforms import DEFAULT_PLATFORM, normalize_model, normalize_platform
from . import postgres


TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUBMITTED = "submitted"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"
TASK_TIMEOUT_HOURS = 3
TASK_RETRY_TIMEOUT_MINUTES = 30
MAX_TASK_RETRIES = 2
LOCAL_TZ = timezone(timedelta(hours=8))
_TASK_LOCKS_LOCK = threading.RLock()
_TASK_LOCKS: dict[str, threading.RLock] = {}
_TASK_CREATE_LOCK = threading.RLock()
TRANSIENT_RESULT_FIELDS = {
    "chat_status",
    "chat_content_type",
    "chat_response_bytes",
    "chat_response_preview",
    "sse_response_text",
    "sse_timed_out",
    "conversation_id",
    "cookie_string",
    "cookies",
    "main_url",
    "decoded_main_url",
    "last_query_error",
    "last_query_error_category",
    "proxy_source",
    "proxy_server",
    "proxy_raw",
    "account_id",
    "account_name",
    "account_quota_charge_id",
    "account_quota_refunded",
}


class CorruptJSONError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_lock(task_id: str) -> threading.RLock:
    task_id = validate_task_id(task_id)
    with _TASK_LOCKS_LOCK:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[task_id] = lock
        return lock


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_local_today(value: str) -> bool:
    parsed = parse_time(value)
    if not parsed:
        return False
    return parsed.astimezone(LOCAL_TZ).date() == datetime.now(LOCAL_TZ).date()


def is_task_expired(meta: dict[str, Any]) -> bool:
    if str(meta.get("status") or "") in {STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELED}:
        return False
    retry_count = max(0, int(meta.get("retry_count") or 0))
    retry_started_at = parse_time(str(meta.get("retry_started_at") or ""))
    created_at = parse_time(str(meta.get("created_at") or ""))
    if retry_count > 0:
        retry_origin = retry_started_at or created_at
        return bool(retry_origin and datetime.now(timezone.utc) - retry_origin >= timedelta(minutes=TASK_RETRY_TIMEOUT_MINUTES))
    if not created_at:
        return False
    return datetime.now(timezone.utc) - created_at >= timedelta(hours=TASK_TIMEOUT_HOURS)


def expire_task_if_timeout(task_id: str) -> bool:
    meta = get_meta(task_id)
    if not is_task_expired(meta):
        return False
    result = load_result(task_id)
    if result.get("decoded_main_url"):
        return False
    retry_expired = max(0, int(meta.get("retry_count") or 0)) > 0
    result_account_id = str(result.get("account_id") or "")
    if result_account_id:
        from .accounts import clear_account_current_task, exhaust_timed_out_account

        exhaust_timed_out_account(result_account_id, str(result.get("account_quota_charge_id") or ""))
        clear_account_current_task(result_account_id, task_id)
    mark_failed(task_id, "重试超过30分钟，生成失败" if retry_expired else "超时生成失败")
    owner_hash = str(meta.get("owner_token_hash") or "")
    if owner_hash:
        from .temp_access import refund_temp_quota_hash

        if refund_temp_quota_hash(owner_hash, task_id):
            mark_result_once(task_id, "temp_quota_refunded", True)
    return True


def validate_task_id(task_id: str) -> str:
    task_id = (task_id or "").strip().lower()
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid task id")
    return task_id


def task_dir(task_id: str) -> Path:
    return TASKS_DIR / validate_task_id(task_id)


def images_dir(task_id: str) -> Path:
    return task_dir(task_id) / "images"


def meta_path(task_id: str) -> Path:
    return task_dir(task_id) / "meta.json"


def result_path(task_id: str) -> Path:
    return task_dir(task_id) / "result.json"


def runtime_path() -> Path:
    from .config import RUNTIME_PATH

    return RUNTIME_PATH


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptJSONError(str(path)) from exc
    if not isinstance(data, dict):
        raise CorruptJSONError(str(path))
    return data


def _task_part(path: Path) -> tuple[str, str] | None:
    if path.name not in {"meta.json", "result.json"}:
        return None
    task_id = path.parent.name
    if not TASK_ID_RE.fullmatch(task_id):
        return None
    return task_id, path.stem


def _read_storage_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if postgres.enabled():
        task_part = _task_part(path)
        if task_part:
            return postgres.read_task_part(task_part[0], task_part[1], default)
        if path == runtime_path():
            return postgres.read_document("runtime", default)
    return read_json(path, default)


def _write_storage_json(path: Path, data: dict[str, Any]) -> None:
    if postgres.enabled():
        task_part = _task_part(path)
        if task_part:
            postgres.write_task_part(task_part[0], task_part[1], data)
            return
        if path == runtime_path():
            postgres.write_document("runtime", data)
            return
    write_json(path, data)


def ensure_storage() -> None:
    global _STORAGE_READY, _STORAGE_SIGNATURE
    ensure_dirs()
    if postgres.enabled():
        signature = (id(postgres.ensure_schema), str(TASKS_DIR))
        if _STORAGE_READY and _STORAGE_SIGNATURE == signature:
            return
        postgres.ensure_schema()
        if not postgres.read_document("runtime"):
            postgres.write_document("runtime", default_runtime())
        _STORAGE_READY = True
        _STORAGE_SIGNATURE = signature
        return
    runtime = runtime_path()
    if not runtime.exists():
        write_json(runtime, default_runtime())


_STORAGE_READY = False
_STORAGE_SIGNATURE: tuple[int, str] | None = None


def count_pending_tasks() -> int:
    ensure_storage()
    if postgres.enabled() and hasattr(postgres, "count_tasks"):
        return postgres.count_tasks("pending")
    return sum(1 for item in list_tasks() if str(item.get("status") or "") == STATUS_PENDING)


def create_task(prompt: str, ratio: str, owner_token_hash: str = "", platform: str = DEFAULT_PLATFORM, model: str = "", task_type: str = "video", enqueue: bool = True, idempotency_hash: str = "", request_fingerprint: str = "", request_route: str = "") -> dict[str, Any]:
    platform = normalize_platform(platform)
    model = normalize_model(model)
    ensure_storage()
    for _ in range(20):
        task_id = secrets.token_hex(16)
        root = task_dir(task_id)
        if not task_exists(task_id):
            with task_lock(task_id):
                root.mkdir(parents=True, exist_ok=True)
                images_dir(task_id).mkdir(exist_ok=True)
                now = utc_now()
                meta = {
                    "id": task_id,
                    "prompt": prompt,
                    "ratio": ratio,
                    "platform": platform,
                    "model": model,
                    "task_type": "image" if task_type == "image" else "video",
                    "status": STATUS_PENDING if enqueue else "initializing",
                    "image_count": 0,
                    "owner_token_hash": owner_token_hash,
                    "created_at": now,
                    "queued_at": now,
                    "claimed_at": "",
                    "attempt": 0,
                    "updated_at": now,
                    "error": "",
                    "idempotency_hash": idempotency_hash,
                    "request_fingerprint": request_fingerprint,
                    "request_route": request_route,
                }
                if postgres.enabled():
                    if postgres.create_task(task_id, meta):
                        from .task_queue import get_task_queue

                        if enqueue:
                            get_task_queue().enqueue(task_id)
                        return meta
                else:
                    write_json(meta_path(task_id), meta)
                    from .task_queue import get_task_queue

                    if enqueue:
                        get_task_queue().enqueue(task_id)
                    return meta
    raise RuntimeError("could not allocate task id")


def find_or_create_task(prompt: str, ratio: str, owner_token_hash: str, platform: str, model: str, task_type: str, idempotency_key: str, request_fingerprint: str, request_route: str) -> tuple[dict[str, Any], bool]:
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    with _TASK_CREATE_LOCK:
        if postgres.enabled():
            platform = normalize_platform(platform)
            model = normalize_model(model)
            ensure_storage()
            task_id = secrets.token_hex(16)
            now = utc_now()
            meta = {
                "id": task_id,
                "prompt": prompt,
                "ratio": ratio,
                "platform": platform,
                "model": model,
                "task_type": "image" if task_type == "image" else "video",
                "status": "initializing",
                "image_count": 0,
                "owner_token_hash": owner_token_hash,
                "created_at": now,
                "queued_at": now,
                "claimed_at": "",
                "attempt": 0,
                "updated_at": now,
                "error": "",
                "idempotency_hash": key_hash,
                "request_fingerprint": request_fingerprint,
                "request_route": request_route,
            }
            return postgres.find_or_create_idempotent_task(task_id, meta)
        for item in list_tasks(owner_token_hash=owner_token_hash or None):
            if str(item.get("idempotency_hash") or "") != key_hash or str(item.get("request_route") or "") != request_route:
                continue
            meta = get_meta(str(item["id"]))
            if str(meta.get("request_fingerprint") or "") != request_fingerprint:
                raise ValueError("idempotency key conflicts with a different request")
            return meta, False
        return create_task(prompt, ratio, owner_token_hash=owner_token_hash, platform=platform, model=model, task_type=task_type, enqueue=False, idempotency_hash=key_hash, request_fingerprint=request_fingerprint, request_route=request_route), True


def finalize_task_creation(task_id: str) -> dict[str, Any]:
    with task_lock(task_id):
        meta = get_meta(task_id)
        if str(meta.get("status") or "") != "initializing":
            raise RuntimeError("task is not initializing")
        meta["status"] = STATUS_PENDING
        meta["queued_at"] = utc_now()
        meta["updated_at"] = meta["queued_at"]
        if postgres.enabled():
            postgres.write_task_part(task_id, "meta", meta)
        else:
            write_json(meta_path(task_id), meta)
        from .task_queue import get_task_queue
        get_task_queue().enqueue(task_id)
        return meta


def fail_initializing_tasks(reason: str = "任务未成功进入队列，请重新提交") -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for item in list_tasks():
        task_id = str(item.get("id") or "")
        if not task_id or str(item.get("status") or "") != "initializing":
            continue
        updated = update_meta_if(
            task_id,
            {"initializing"},
            status=STATUS_FAILED,
            worker_id="",
            finished_at=utc_now(),
            error=reason,
        )
        if updated:
            failed.append(updated)
    return failed


def set_task_images(task_id: str, paths: Iterable[Path]) -> None:
    with task_lock(task_id):
        image_count = len(list(paths))
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> None:
                meta["image_count"] = image_count
                meta["updated_at"] = utc_now()

            postgres.mutate_task_part(task_id, "meta", mutate)
            return
        meta = get_meta(task_id)
        meta["image_count"] = image_count
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)


def get_meta(task_id: str) -> dict[str, Any]:
    with task_lock(task_id):
        if postgres.enabled():
            return postgres.read_task_part(task_id, "meta")
        path = meta_path(task_id)
        if not path.exists():
            raise FileNotFoundError(task_id)
        return read_json(path)


def task_exists(task_id: str) -> bool:
    try:
        task_id = validate_task_id(task_id)
        return postgres.task_exists(task_id) if postgres.enabled() else meta_path(task_id).exists()
    except ValueError:
        return False


def update_meta(task_id: str, **updates: Any) -> dict[str, Any]:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> dict[str, Any]:
                meta.update(updates)
                meta["updated_at"] = utc_now()
                return dict(meta)

            return postgres.mutate_task_part(task_id, "meta", mutate)
        meta = get_meta(task_id)
        meta.update(updates)
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)
        return meta


def update_meta_if(task_id: str, expected_statuses: set[str], **updates: Any) -> dict[str, Any] | None:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> dict[str, Any] | None:
                if str(meta.get("status") or "") not in expected_statuses:
                    return None
                meta.update(updates)
                meta["updated_at"] = utc_now()
                return dict(meta)

            return postgres.mutate_task_part(task_id, "meta", mutate)
        meta = get_meta(task_id)
        if str(meta.get("status") or "") not in expected_statuses:
            return None
        meta.update(updates)
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)
        return meta


def mark_running(task_id: str, worker_id: str, concurrency_limits: dict[str, int] | None = None) -> bool:
    limits = concurrency_limits or {}
    claimed_at = utc_now()
    with task_lock(task_id):
        if postgres.enabled():
            meta = postgres.read_task_part(task_id, "meta")
            owner = str(meta.get("owner_token_hash") or "")
            limit = limits.get(owner) if owner else None
            return postgres.claim_task(task_id, worker_id, owner, limit, claimed_at)
        meta = get_meta(task_id)
        if str(meta.get("status") or "") != STATUS_PENDING or bool(meta.get("cancel_requested")):
            return False
        meta.update(status=STATUS_RUNNING, worker_id=worker_id, started_at=claimed_at, claimed_at=claimed_at, attempt=max(0, int(meta.get("attempt") or 0)) + 1, error="", execution_miss_count=0, submit_phase="", submit_started_at="", updated_at=claimed_at)
        _write_storage_json(meta_path(task_id), meta)
        return True


def mark_pending(task_id: str, reason: str = "") -> None:
    update_meta_if(task_id, {STATUS_PENDING, STATUS_RUNNING}, status=STATUS_PENDING, worker_id="", queued_at=utc_now(), error=reason)


def mark_failed(task_id: str, reason: str = "") -> None:
    update_meta_if(task_id, {"initializing", STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED, STATUS_FAILED}, status=STATUS_FAILED, worker_id="", finished_at=utc_now(), error=reason)


def mark_canceled(task_id: str, reason: str = "canceled") -> None:
    update_meta_if(task_id, {STATUS_PENDING, STATUS_RUNNING}, status=STATUS_CANCELED, worker_id="", finished_at=utc_now(), error=reason)


def mark_cancel_requested(task_id: str, reason: str = "cancel requested") -> None:
    update_meta(task_id, cancel_requested=True, error=reason)


def can_run_task(task_id: str, worker_id: str) -> bool:
    with task_lock(task_id):
        meta = get_meta(task_id)
        return str(meta.get("status") or "") == STATUS_RUNNING and str(meta.get("worker_id") or "") == str(worker_id or "") and not bool(meta.get("cancel_requested"))


def begin_task_submission(task_id: str) -> bool:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> bool:
                if str(meta.get("status") or "") != STATUS_RUNNING or bool(meta.get("cancel_requested")) or str(meta.get("submit_phase") or "") == "committing":
                    return False
                meta["submit_phase"] = "committing"
                meta["submit_started_at"] = utc_now()
                meta["updated_at"] = meta["submit_started_at"]
                return True

            return bool(postgres.mutate_task_part(task_id, "meta", mutate))
        meta = get_meta(task_id)
        if str(meta.get("status") or "") != STATUS_RUNNING or bool(meta.get("cancel_requested")) or str(meta.get("submit_phase") or "") == "committing":
            return False
        meta["submit_phase"] = "committing"
        meta["submit_started_at"] = utc_now()
        meta["updated_at"] = meta["submit_started_at"]
        _write_storage_json(meta_path(task_id), meta)
        return True


def release_task_submission(task_id: str) -> None:
    update_meta_if(task_id, {STATUS_RUNNING}, submit_phase="", submit_started_at="")


def request_task_cancel(task_id: str, reason: str = "用户取消生成") -> tuple[bool, dict[str, Any]]:
    with task_lock(task_id):
        if postgres.enabled():
            outcome: dict[str, Any] = {}

            def mutate(meta: dict[str, Any]) -> dict[str, Any]:
                outcome.update(meta)
                if str(meta.get("status") or "") not in {STATUS_PENDING, STATUS_RUNNING} or str(meta.get("submit_phase") or "") == "committing":
                    return dict(meta)
                meta.update(status=STATUS_CANCELED, worker_id="", finished_at=utc_now(), error=reason, updated_at=utc_now())
                outcome.clear()
                outcome.update(meta)
                outcome["cancel_applied"] = True
                return dict(meta)

            postgres.mutate_task_part(task_id, "meta", mutate)
            return bool(outcome.pop("cancel_applied", False)), outcome
        meta = get_meta(task_id)
        if str(meta.get("status") or "") not in {STATUS_PENDING, STATUS_RUNNING} or str(meta.get("submit_phase") or "") == "committing":
            return False, meta
        meta.update(status=STATUS_CANCELED, worker_id="", finished_at=utc_now(), error=reason, updated_at=utc_now())
        _write_storage_json(meta_path(task_id), meta)
        return True, meta


def record_failed_account(task_id: str, account_id: str) -> None:
    account_id = str(account_id or "")
    if not account_id:
        return
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> None:
                failed = [str(item) for item in meta.get("failed_account_ids") or [] if item]
                if account_id not in failed:
                    failed.append(account_id)
                meta["failed_account_ids"] = failed
                meta["updated_at"] = utc_now()

            postgres.mutate_task_part(task_id, "meta", mutate)
            return
        meta = get_meta(task_id)
        failed = [str(item) for item in meta.get("failed_account_ids") or [] if item]
        if account_id not in failed:
            failed.append(account_id)
        meta["failed_account_ids"] = failed
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)


def record_retry(task_id: str, reason: str = "") -> int:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> int:
                if str(meta.get("status") or "") in {STATUS_SUCCESS, STATUS_SUBMITTED, STATUS_FAILED, STATUS_CANCELED}:
                    return max(0, int(meta.get("retry_count") or 0))
                count = max(0, int(meta.get("retry_count") or 0)) + 1
                normalized_reason = "浏览器超时" if str(reason or "") == "browser timeout" else "Dola 当前地区不可用" if str(reason or "") == "region restricted" else reason
                meta.update(retry_count=count, worker_id="", error=normalized_reason)
                meta.setdefault("retry_started_at", utc_now())
                if count > MAX_TASK_RETRIES:
                    meta.update(status=STATUS_FAILED, finished_at=utc_now())
                else:
                    meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=10 * (3 ** (count - 1)))).isoformat())
                meta["updated_at"] = utc_now()
                return count

            return postgres.mutate_task_part(task_id, "meta", mutate)
        meta = get_meta(task_id)
        if str(meta.get("status") or "") in {STATUS_SUCCESS, STATUS_SUBMITTED, STATUS_FAILED, STATUS_CANCELED}:
            return max(0, int(meta.get("retry_count") or 0))
        count = max(0, int(meta.get("retry_count") or 0)) + 1
        if str(reason or "") == "browser timeout":
            reason = "浏览器超时"
        if str(reason or "") == "region restricted":
            reason = "Dola 当前地区不可用"
        meta.update(retry_count=count, worker_id="", error=reason)
        meta.setdefault("retry_started_at", utc_now())
        if count > MAX_TASK_RETRIES:
            meta.update(status=STATUS_FAILED, finished_at=utc_now())
        else:
            meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=10 * (3 ** (count - 1)))).isoformat())
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)
        return count


def requeue_pending_task(task_id: str) -> bool:
    meta = get_meta(task_id)
    if str(meta.get("status") or "") != STATUS_PENDING or bool(meta.get("cancel_requested")):
        return False
    from .task_queue import get_task_queue

    available_at = parse_time(str(meta.get("next_attempt_at") or ""))
    return get_task_queue().requeue(task_id, available_at)


def retry_submitted_task(task_id: str, reason: str, max_retries: int = MAX_TASK_RETRIES, delay_seconds: int = 45) -> int:
    max_retries = max(1, min(MAX_TASK_RETRIES, int(max_retries)))
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> int:
                if str(meta.get("status") or "") != STATUS_SUBMITTED:
                    return max(0, int(meta.get("retry_count") or 0))
                count = max(0, int(meta.get("retry_count") or 0)) + 1
                meta.update(retry_count=count, worker_id="", error=reason, result_watch_miss_count=0)
                meta.setdefault("retry_started_at", utc_now())
                if count > max_retries:
                    meta.update(status=STATUS_FAILED, finished_at=utc_now())
                else:
                    meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds * count)).isoformat())
                meta["updated_at"] = utc_now()
                return count

            count = postgres.mutate_task_part(task_id, "meta", mutate)
            if count <= max_retries:
                requeue_pending_task(task_id)
            return count
        meta = get_meta(task_id)
        if str(meta.get("status") or "") != STATUS_SUBMITTED:
            return max(0, int(meta.get("retry_count") or 0))
        count = max(0, int(meta.get("retry_count") or 0)) + 1
        meta.update(retry_count=count, worker_id="", error=reason, result_watch_miss_count=0)
        meta.setdefault("retry_started_at", utc_now())
        if count > max_retries:
            meta.update(status=STATUS_FAILED, finished_at=utc_now())
        else:
            meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds * count)).isoformat())
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)
        if count <= max_retries:
            requeue_pending_task(task_id)
        return count


def retry_timed_out_submitted_task(task_id: str, reason: str, max_retries: int = MAX_TASK_RETRIES, delay_seconds: int = 10) -> int:
    max_retries = max(1, min(MAX_TASK_RETRIES, int(max_retries)))
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(meta: dict[str, Any]) -> int:
                if str(meta.get("status") or "") != STATUS_SUBMITTED or bool(meta.get("cancel_requested")):
                    return max(0, int(meta.get("result_timeout_retry_count") or 0))
                previous_timeout_count = max(0, int(meta.get("result_timeout_retry_count") or 0))
                timeout_count = previous_timeout_count + 1
                count = max(max(0, int(meta.get("retry_count") or 0)), previous_timeout_count) + 1
                meta.update(retry_count=count, result_timeout_retry_count=timeout_count, retry_queued_at=utc_now(), worker_id="", error=reason, result_watch_miss_count=0)
                meta.setdefault("retry_started_at", utc_now())
                if count > max_retries:
                    meta.update(status=STATUS_FAILED, finished_at=utc_now())
                else:
                    meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds * count)).isoformat())
                meta["updated_at"] = utc_now()
                return count

            count = postgres.mutate_task_part(task_id, "meta", mutate)
            if count <= max_retries:
                requeue_pending_task(task_id)
            return count
        meta = get_meta(task_id)
        if str(meta.get("status") or "") != STATUS_SUBMITTED or bool(meta.get("cancel_requested")):
            return max(0, int(meta.get("result_timeout_retry_count") or 0))
        previous_timeout_count = max(0, int(meta.get("result_timeout_retry_count") or 0))
        timeout_count = previous_timeout_count + 1
        count = max(max(0, int(meta.get("retry_count") or 0)), previous_timeout_count) + 1
        meta.update(retry_count=count, result_timeout_retry_count=timeout_count, retry_queued_at=utc_now(), worker_id="", error=reason, result_watch_miss_count=0)
        meta.setdefault("retry_started_at", utc_now())
        if count > max_retries:
            meta.update(status=STATUS_FAILED, finished_at=utc_now())
        else:
            meta.update(status=STATUS_PENDING, finished_at="", next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds * count)).isoformat())
        meta["updated_at"] = utc_now()
        _write_storage_json(meta_path(task_id), meta)
        if count <= max_retries:
            requeue_pending_task(task_id)
        return count


def record_execution_miss(task_id: str, reason: str = "任务未执行，重新排队") -> int:
    if postgres.enabled():
        def mutate(meta: dict[str, Any]) -> int:
            miss_count = max(0, int(meta.get("execution_miss_count") or 0)) + 1
            count = max(0, int(meta.get("retry_count") or 0)) + 1
            meta.setdefault("retry_started_at", utc_now())
            if count > MAX_TASK_RETRIES:
                meta.update(retry_count=count, execution_miss_count=miss_count, worker_id="", error="任务超时未执行", status=STATUS_FAILED, finished_at=utc_now())
            else:
                meta.update(retry_count=count, execution_miss_count=miss_count, worker_id="", error=reason, status=STATUS_PENDING)
            meta["updated_at"] = utc_now()
            return count

        return postgres.mutate_task_part(task_id, "meta", mutate)
    meta = get_meta(task_id)
    miss_count = max(0, int(meta.get("execution_miss_count") or 0)) + 1
    count = max(0, int(meta.get("retry_count") or 0)) + 1
    retry_started_at = str(meta.get("retry_started_at") or utc_now())
    if count > MAX_TASK_RETRIES:
        update_meta(task_id, retry_count=count, retry_started_at=retry_started_at, execution_miss_count=miss_count, worker_id="", error="任务超时未执行", status=STATUS_FAILED, finished_at=utc_now())
    else:
        update_meta(task_id, retry_count=count, retry_started_at=retry_started_at, execution_miss_count=miss_count, worker_id="", error=reason, status=STATUS_PENDING)
    return count


def record_result_watch_miss(task_id: str, reason: str = "生成结果未完成") -> int:
    if postgres.enabled():
        def mutate(meta: dict[str, Any]) -> int:
            count = max(0, int(meta.get("result_watch_miss_count") or 0)) + 1
            meta.update(result_watch_miss_count=count, error=reason, updated_at=utc_now())
            return count

        return postgres.mutate_task_part(task_id, "meta", mutate)
    meta = get_meta(task_id)
    count = max(0, int(meta.get("result_watch_miss_count") or 0)) + 1
    update_meta(task_id, result_watch_miss_count=count, error=reason)
    return count


def mark_success(task_id: str) -> None:
    result = load_result(task_id)
    if not result.get("decoded_main_url"):
        return
    update_meta_if(task_id, {STATUS_RUNNING, STATUS_SUBMITTED, STATUS_SUCCESS}, status=STATUS_SUCCESS, worker_id="", finished_at=utc_now(), error="")


def mark_submitted(task_id: str) -> None:
    update_meta_if(task_id, {STATUS_RUNNING}, status=STATUS_SUBMITTED, worker_id="", submitted_at=utc_now(), finished_at="", error="等待生成结果", result_watch_miss_count=0, submit_phase="submitted")


def save_result(
    task_id: str,
    *,
    conversation_id: str = "",
    cookie_string: str = "",
    cookies: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    remove: Iterable[str] | None = None,
) -> None:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                if conversation_id:
                    data["conversation_id"] = conversation_id
                if cookie_string:
                    data["cookie_string"] = cookie_string
                if cookies is not None:
                    data["cookies"] = cookies
                if extra:
                    data.update(extra)
                if remove:
                    for key in remove:
                        data.pop(str(key), None)
                data["updated_at"] = utc_now()

            try:
                postgres.mutate_task_part(task_id, "result", mutate)
            except FileNotFoundError:
                return
            return
        if not task_exists(task_id):
            return
        data = _read_storage_json(result_path(task_id), {})
        if conversation_id:
            data["conversation_id"] = conversation_id
        if cookie_string:
            data["cookie_string"] = cookie_string
        if cookies is not None:
            data["cookies"] = cookies
        if extra:
            data.update(extra)
        if remove:
            for key in remove:
                data.pop(str(key), None)
        data["updated_at"] = utc_now()
        _write_storage_json(result_path(task_id), data)


def mark_result_once(task_id: str, key: str, value: Any = True) -> bool:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                if data.get(key):
                    return False
                data[key] = value
                data["updated_at"] = utc_now()
                return True

            return postgres.mutate_task_part(task_id, "result", mutate)
        if not task_exists(task_id):
            return False
        data = _read_storage_json(result_path(task_id), {})
        if data.get(key):
            return False
        data[key] = value
        data["updated_at"] = utc_now()
        _write_storage_json(result_path(task_id), data)
        return True


def mark_account_refund_once(task_id: str, account_id: str) -> bool:
    account_id = str(account_id or "")
    if not account_id:
        return False
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                refunded = [str(item) for item in data.get("account_quota_refunded_ids") or [] if item]
                if account_id in refunded:
                    return False
                refunded.append(account_id)
                data["account_quota_refunded_ids"] = refunded
                data["account_quota_refunded"] = True
                data["updated_at"] = utc_now()
                return True

            try:
                return postgres.mutate_task_part(task_id, "result", mutate)
            except FileNotFoundError:
                return False
        if not task_exists(task_id):
            return False
        data = _read_storage_json(result_path(task_id), {})
        refunded = [str(item) for item in data.get("account_quota_refunded_ids") or [] if item]
        if account_id in refunded:
            return False
        refunded.append(account_id)
        data["account_quota_refunded_ids"] = refunded
        data["account_quota_refunded"] = True
        data["updated_at"] = utc_now()
        _write_storage_json(result_path(task_id), data)
        return True


def clear_transient_result(task_id: str) -> None:
    with task_lock(task_id):
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                changed = False
                for key in TRANSIENT_RESULT_FIELDS:
                    if key in data:
                        data.pop(key, None)
                        changed = True
                if changed:
                    data["updated_at"] = utc_now()

            try:
                postgres.mutate_task_part(task_id, "result", mutate)
            except FileNotFoundError:
                return
            return
        if not task_exists(task_id):
            return
        path = result_path(task_id)
        data = _read_storage_json(path, {})
        if not data:
            return
        changed = False
        for key in TRANSIENT_RESULT_FIELDS:
            if key in data:
                data.pop(key, None)
                changed = True
        if changed:
            data["updated_at"] = utc_now()
            _write_storage_json(path, data)


def load_result(task_id: str) -> dict[str, Any]:
    with task_lock(task_id):
        return _read_storage_json(result_path(task_id), {})


def is_active_status(status: str) -> bool:
    return str(status or "") in {STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED, STATUS_SUCCESS}


def task_has_video(task_id: str) -> bool:
    return bool(load_result(task_id).get("decoded_main_url"))


def set_task_video_hidden(task_id: str, audience: str, hidden: bool = True) -> None:
    if audience not in {"admin", "client"}:
        raise ValueError("invalid audience")
    update_meta(task_id, **{f"video_hidden_for_{audience}": bool(hidden)})


def set_task_hidden(task_id: str, audience: str, hidden: bool = True) -> None:
    if audience not in {"admin", "client"}:
        raise ValueError("invalid audience")
    update_meta(task_id, **{f"task_hidden_for_{audience}": bool(hidden)})


def active_task_count_for_owner(owner_token_hash: str) -> int:
    owner_token_hash = str(owner_token_hash or "")
    if not owner_token_hash:
        return 0
    count = 0
    for item in list_tasks(owner_token_hash=owner_token_hash):
        task_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        if status in {STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED} or (status == STATUS_SUCCESS and not task_has_video(task_id)):
            count += 1
    return count


def account_active_tasks() -> dict[str, list[dict[str, str]]]:
    active: dict[str, list[dict[str, str]]] = {}
    for item in list_tasks():
        task_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        if not task_id or not is_active_status(status):
            continue
        result = load_result(task_id)
        account_id = str(result.get("account_id") or "")
        if not account_id:
            continue
        if status == STATUS_SUCCESS and result.get("decoded_main_url"):
            continue
        active.setdefault(account_id, []).append({"task_id": task_id, "status": status, "worker_id": str(item.get("worker_id") or "")})
    return active


def task_image_paths(task_id: str) -> list[Path]:
    root = images_dir(task_id)
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_file()])


def list_tasks(owner_token_hash: str | None = None, owner_remarks: dict[str, str] | None = None) -> list[dict[str, Any]]:
    ensure_storage()
    items: list[dict[str, Any]] = []
    remarks = owner_remarks or {}
    if postgres.enabled() and hasattr(postgres, "list_task_metas"):
        task_items = postgres.list_task_metas(owner_token_hash)
    else:
        task_items = [(path.name, None) for path in TASKS_DIR.iterdir() if path.is_dir()] if not postgres.enabled() else [(task_id, None) for task_id in postgres.list_task_ids()]
    for task_id, stored_meta in task_items:
        if not TASK_ID_RE.fullmatch(task_id):
            continue
        try:
            meta = dict(stored_meta) if stored_meta is not None else get_meta(task_id)
        except (FileNotFoundError, CorruptJSONError):
            continue
        if is_task_expired(meta):
            expire_task_if_timeout(task_id)
            meta = get_meta(task_id)
        if owner_token_hash is not None and str(meta.get("owner_token_hash") or "") != owner_token_hash:
            continue
        prompt = str(meta.get("prompt") or "")
        owner = str(meta.get("owner_token_hash") or "")
        items.append(
            {
                "id": task_id,
                "prompt": prompt,
                "prompt_preview": prompt[:15],
                "platform": str(meta.get("platform") or DEFAULT_PLATFORM),
                "model": str(meta.get("model") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "updated_at": str(meta.get("updated_at") or ""),
                "idempotency_hash": str(meta.get("idempotency_hash") or ""),
                "request_fingerprint": str(meta.get("request_fingerprint") or ""),
                "request_route": str(meta.get("request_route") or ""),
                "finished_at": str(meta.get("finished_at") or ""),
                "completed_today": is_local_today(str(meta.get("finished_at") or "")),
                "status": str(meta.get("status") or ""),
                "retry_count": int(meta.get("retry_count") or 0),
                "queued_at": str(meta.get("queued_at") or meta.get("created_at") or ""),
                "claimed_at": str(meta.get("claimed_at") or ""),
                "attempt": int(meta.get("attempt") or 0),
                "worker_id": str(meta.get("worker_id") or ""),
                "image_count": int(meta.get("image_count") or 0),
                "error": str(meta.get("error") or ""),
                "owner_token_hash": owner,
                "owner_name": remarks.get(owner, "") if owner else "管理员",
                "video_hidden_for_admin": bool(meta.get("video_hidden_for_admin", False)),
                "video_hidden_for_client": bool(meta.get("video_hidden_for_client", False)),
                "task_hidden_for_admin": bool(meta.get("task_hidden_for_admin", False)),
                "task_hidden_for_client": bool(meta.get("task_hidden_for_client", False)),
            }
        )
    items.sort(
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    return items


def delete_task(task_id: str) -> None:
    with task_lock(task_id):
        if postgres.enabled():
            postgres.delete_task(validate_task_id(task_id))
        root = task_dir(task_id)
        if root.exists():
            shutil.rmtree(root)


def migrate_task_owner(old_token_hash: str, new_token_hash: str) -> int:
    ensure_storage()
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    changed = 0
    for item in list_tasks(owner_token_hash=old_token_hash):
        update_meta(str(item["id"]), owner_token_hash=new_token_hash)
        changed += 1
    return changed


def is_task_canceled(task_id: str) -> bool:
    try:
        meta = get_meta(task_id)
        return str(meta.get("status") or "") == STATUS_CANCELED or bool(meta.get("cancel_requested"))
    except FileNotFoundError:
        return True


def delete_inactive_tasks(active_ids: set[str] | None = None, owner_token_hash: str | None = None) -> dict[str, Any]:
    ensure_storage()
    active = active_ids or set()
    deleted = 0
    skipped: list[str] = []
    for item in list_tasks(owner_token_hash=owner_token_hash):
        task_id = item["id"]
        status = str(item.get("status") or "")
        if task_id in active or status in {STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED} or (status == STATUS_SUCCESS and not task_has_video(task_id)):
            skipped.append(task_id)
            continue
        delete_task(task_id)
        deleted += 1
    return {"deleted": deleted, "skipped": skipped}


def cleanup_expired_task_cache(retention_days: int = 7, active_ids: set[str] | None = None, owner_retention_days: dict[str, int] | None = None) -> dict[str, Any]:
    ensure_storage()
    active = active_ids or set()
    deleted = 0
    skipped: list[str] = []
    owner_retention_days = owner_retention_days or {}
    for item in list_tasks():
        task_id = item["id"]
        status = str(item.get("status") or "")
        owner = str(item.get("owner_token_hash") or "")
        task_retention_days = owner_retention_days.get(owner, retention_days) if owner else retention_days
        task_threshold = datetime.now(timezone.utc) - timedelta(days=max(1, int(task_retention_days or retention_days or 7)))
        if task_id in active or status in {STATUS_PENDING, STATUS_RUNNING, STATUS_SUBMITTED}:
            skipped.append(task_id)
            continue
        if status == STATUS_SUCCESS and not task_has_video(task_id):
            skipped.append(task_id)
            continue
        finished_at = parse_time(str(item.get("finished_at") or ""))
        updated_at = parse_time(str(item.get("updated_at") or ""))
        created_at = parse_time(str(item.get("created_at") or ""))
        reference_time = finished_at or updated_at or created_at
        if not reference_time or reference_time > task_threshold:
            continue
        delete_task(task_id)
        deleted += 1
    return {"deleted": deleted, "skipped": skipped}


def reset_running_tasks() -> None:
    ensure_storage()
    for item in list_tasks():
        task_id = item["id"]
        try:
            meta = get_meta(task_id)
        except (FileNotFoundError, CorruptJSONError):
            continue
        if meta.get("status") == STATUS_RUNNING:
            result = load_result(task_id)
            submitted = bool(
                result.get("conversation_id")
                or result.get("doubao_submit_confirmed")
                or result.get("qianwen_submit_confirmed")
                or result.get("qianwen_remote_task_ids")
            )
            if submitted:
                update_meta_if(task_id, {STATUS_RUNNING}, status=STATUS_SUBMITTED, worker_id="", submitted_at=str(meta.get("submitted_at") or utc_now()), finished_at="", error="等待生成结果")
            else:
                mark_pending(task_id, "service restarted")


def claim_next_pending(worker_id: str, claimed_ids: set[str], token_active_counts: dict[str, int] | None = None, token_concurrency_limits: dict[str, int] | None = None) -> str | None:
    ensure_storage()
    concurrency_limits = token_concurrency_limits or {}
    claim_lock = TASKS_DIR / ".claim.lock"
    deadline = time.monotonic() + 5
    while True:
        try:
            claim_lock.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - claim_lock.stat().st_mtime > 30:
                    claim_lock.rmdir()
                    continue
            except (FileNotFoundError, OSError):
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
    try:
        active_counts: dict[str, int] = {
            str(owner): max(0, int(count))
            for owner, count in (token_active_counts or {}).items()
            if owner
        }
        candidates: list[dict[str, Any]] = []
        for item in list_tasks():
            task_id = str(item["id"])
            try:
                meta = get_meta(task_id)
            except (FileNotFoundError, CorruptJSONError):
                continue
            owner = str(meta.get("owner_token_hash") or "")
            if str(meta.get("status") or "") == STATUS_RUNNING and owner:
                active_counts[owner] = max(1, active_counts.get(owner, 0))
            if task_id not in claimed_ids and str(meta.get("status") or "") == STATUS_PENDING:
                candidates.append(meta)
        candidates.sort(key=lambda meta: (str(meta.get("queued_at") or meta.get("created_at") or ""), str(meta.get("created_at") or ""), str(meta.get("id") or "")))
        for meta in candidates:
            task_id = str(meta["id"])
            next_attempt_at = parse_time(str(meta.get("next_attempt_at") or ""))
            if next_attempt_at and datetime.now(timezone.utc) < next_attempt_at:
                continue
            if is_task_expired(meta):
                expire_task_if_timeout(task_id)
                continue
            owner = str(meta.get("owner_token_hash") or "")
            if owner and owner in concurrency_limits and active_counts.get(owner, 0) >= concurrency_limits[owner]:
                continue
            if not meta.get("cancel_requested") and mark_running(task_id, worker_id, concurrency_limits):
                return task_id
        return None
    finally:
        try:
            claim_lock.rmdir()
        except FileNotFoundError:
            pass


def has_pending_tasks(claimed_ids: set[str] | None = None) -> bool:
    ensure_storage()
    claimed = claimed_ids or set()
    for item in list_tasks():
        task_id = item["id"]
        if task_id in claimed:
            continue
        try:
            meta = get_meta(task_id)
        except FileNotFoundError:
            continue
        if meta.get("status") == STATUS_PENDING:
            return True
    return False


def default_runtime() -> dict[str, Any]:
    return {
        "active_task_ids": [],
    }


def load_runtime() -> dict[str, Any]:
    ensure_storage()
    try:
        data = _read_storage_json(runtime_path(), default_runtime())
    except CorruptJSONError:
        data = default_runtime()
        save_runtime(data)
    active = data.get("active_task_ids")
    if not isinstance(active, list):
        active = []
    return {"active_task_ids": [str(item) for item in active]}


def save_runtime(data: dict[str, Any]) -> None:
    active = data.get("active_task_ids")
    if not isinstance(active, list):
        active = []
    _write_storage_json(runtime_path(), {"active_task_ids": sorted({str(item) for item in active})})


def set_active_tasks(task_ids: Iterable[str]) -> None:
    data = load_runtime()
    data["active_task_ids"] = sorted(set(task_ids))
    save_runtime(data)


def active_task_ids() -> set[str]:
    return set(load_runtime().get("active_task_ids") or [])
