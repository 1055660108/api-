from __future__ import annotations

import json
import secrets
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import postgres
from . import config
from .config import ensure_dirs
from .task_queue import get_task_queue


ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}
TERMINAL_ROW_STATUSES = {"completed", "failed", "canceled"}
_LOCK = threading.RLock()
_SCHEDULER_LOCK = threading.Lock()
_LOCAL_OWNER_CURSOR = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_path() -> Path:
    return config.DATA_DIR / "batch_jobs.json"


def _read_local() -> dict[str, Any]:
    ensure_dirs()
    path = _jobs_path()
    if not path.exists():
        return {"jobs": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"jobs": {}}
    return payload if isinstance(payload, dict) and isinstance(payload.get("jobs"), dict) else {"jobs": {}}


def _write_local(payload: dict[str, Any]) -> None:
    ensure_dirs()
    path = _jobs_path()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def create_job(owner_token_hash: str, rows: list[dict[str, Any]], *, ratio: str, concurrency: int, reference_id: str = "", reference_count: int = 0, reference_batch_id: str = "", job_id: str = "") -> dict[str, Any]:
    job_id = str(job_id or "").strip().lower() or secrets.token_hex(16)
    if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
        raise ValueError("invalid batch job id")
    created_at = _now()
    normalized_rows = []
    for index, row in enumerate(rows, start=1):
        normalized_rows.append({
            "index": index,
            "client_index": max(0, int(row.get("client_index") or index - 1)),
            "sheet_row": max(1, int(row.get("sheet_row") or index)),
            "prompt": str(row.get("prompt") or "").strip()[:4000],
            "image_files": [str(name) for name in row.get("image_files", []) if str(name)],
            "image_names": [str(name).strip()[:180] for name in row.get("image_names", []) if str(name).strip()],
            "image_count": max(0, int(row.get("image_count") or 0)),
            "status": "queued",
            "task_id": "",
            "error": "",
            "video_url": "",
            "created_at": "",
            "finished_at": "",
            "revision": 1,
            "updated_at": created_at,
        })
    job = {
        "id": job_id,
        "owner_token_hash": str(owner_token_hash),
        "status": "queued",
        "ratio": str(ratio),
        "duration": 15,
        "platform": "dola",
        "model": "Seedance 2.0",
        "concurrency": max(1, int(concurrency)),
        "reference_id": str(reference_id or ""),
        "reference_count": max(0, int(reference_count or 0)),
        "reference_batch_id": str(reference_batch_id or "")[:100],
        "rows": normalized_rows,
        "revision": 1,
        "created_at": created_at,
        "updated_at": created_at,
        "canceled_at": "",
    }
    if postgres.enabled():
        if not postgres.create_batch_job(job_id, str(owner_token_hash), job):
            raise RuntimeError("could not allocate batch job id")
    else:
        with _LOCK:
            payload = _read_local()
            payload["jobs"][job_id] = job
            _write_local(payload)
    try:
        coordinator().register_owner(str(owner_token_hash))
    except Exception:
        # The scheduler rebuilds eligible owners from persistent jobs on every tick.
        pass
    return deepcopy(job)


def get_job(job_id: str, owner_token_hash: str = "") -> dict[str, Any] | None:
    if postgres.enabled():
        job = postgres.read_batch_job(str(job_id))
    else:
        with _LOCK:
            job = _read_local()["jobs"].get(str(job_id))
    if not isinstance(job, dict):
        return None
    if owner_token_hash and str(job.get("owner_token_hash") or "") != str(owner_token_hash):
        return None
    return deepcopy(job)


def list_jobs(owner_token_hash: str | None = None, *, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    if postgres.enabled():
        return postgres.list_batch_jobs(owner_token_hash, active_only=active_only, limit=limit)
    with _LOCK:
        rows = [deepcopy(item) for item in _read_local()["jobs"].values() if isinstance(item, dict)]
    if owner_token_hash is not None:
        rows = [item for item in rows if str(item.get("owner_token_hash") or "") == str(owner_token_hash)]
    if active_only:
        rows = [item for item in rows if str(item.get("status") or "") in ACTIVE_JOB_STATUSES]
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    return rows[:max(1, min(1000, int(limit)))]


def _mutate_job(job_id: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
    def apply(job: dict[str, Any]):
        before = deepcopy(job)
        result = mutator(job)
        before_rows = {
            int(row.get("index") or 0): row
            for row in before.get("rows", [])
            if isinstance(row, dict)
        }
        changed_rows = []
        for row in job.get("rows", []):
            if not isinstance(row, dict):
                continue
            previous = before_rows.get(int(row.get("index") or 0), {})
            current_payload = {key: value for key, value in row.items() if key not in {"revision", "updated_at"}}
            previous_payload = {key: value for key, value in previous.items() if key not in {"revision", "updated_at"}}
            if current_payload != previous_payload:
                changed_rows.append(row)
        current_job_payload = {key: value for key, value in job.items() if key not in {"rows", "revision", "updated_at"}}
        previous_job_payload = {key: value for key, value in before.items() if key not in {"rows", "revision", "updated_at"}}
        if changed_rows or current_job_payload != previous_job_payload:
            revision = max(0, int(before.get("revision") or 0)) + 1
            updated_at = _now()
            job["revision"] = revision
            job["updated_at"] = updated_at
            for row in changed_rows:
                row["revision"] = revision
                row["updated_at"] = updated_at
        return result

    if postgres.enabled():
        return postgres.mutate_batch_job(str(job_id), apply)
    with _LOCK:
        payload = _read_local()
        job = payload["jobs"].get(str(job_id))
        if not isinstance(job, dict):
            raise KeyError(job_id)
        before = deepcopy(job)
        result = apply(job)
        if job != before:
            _write_local(payload)
        return result


def public_job(job: dict[str, Any], since_revision: int | None = None) -> dict[str, Any]:
    rows = []
    counts = {"queued": 0, "creating": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
    revision = max(0, int(job.get("revision") or 0))
    full = since_revision is None or int(since_revision) > revision
    for raw in job.get("rows", []):
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "queued")
        counts[status] = counts.get(status, 0) + 1
        if not full and int(raw.get("revision") or 0) <= int(since_revision or 0):
            continue
        rows.append({
            "index": int(raw.get("index") or 0),
            "client_index": int(raw.get("client_index") or 0),
            "sheet_row": int(raw.get("sheet_row") or 0),
            "prompt": str(raw.get("prompt") or ""),
            "status": status,
            "task_id": str(raw.get("task_id") or ""),
            "error": str(raw.get("error") or ""),
            "video_url": str(raw.get("video_url") or ""),
            "image_count": int(raw.get("image_count") or 0),
            "revision": max(0, int(raw.get("revision") or 0)),
        })
    return {
        "id": str(job.get("id") or ""),
        "status": str(job.get("status") or "queued"),
        "ratio": str(job.get("ratio") or "9:16"),
        "duration": int(job.get("duration") or 15),
        "concurrency": int(job.get("concurrency") or 1),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
        "revision": revision,
        "delta": not full,
        "total": sum(counts.values()),
        "counts": counts,
        "rows": rows,
    }


def cancel_job(job_id: str, owner_token_hash: str) -> dict[str, Any]:
    def mutate(job: dict[str, Any]):
        if str(job.get("owner_token_hash") or "") != str(owner_token_hash):
            raise PermissionError(job_id)
        job["canceled_at"] = _now()
        for row in job.get("rows", []):
            if str(row.get("status") or "") in {"queued", "creating"} and not str(row.get("task_id") or ""):
                row.update(status="canceled", error="用户停止批量生成", finished_at=_now())
        job["status"] = "canceling" if any(str(row.get("status") or "") == "running" for row in job.get("rows", [])) else "canceled"
        return job

    return _mutate_job(job_id, mutate)


def recover_stale_creating_rows(max_age_seconds: int = 60) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(10, int(max_age_seconds)))
    recovered = 0
    for job in list_jobs(active_only=True, limit=1000):
        if not any(str(row.get("status") or "") == "creating" for row in job.get("rows", []) if isinstance(row, dict)):
            continue
        job_id = str(job.get("id") or "")

        def mutate(current: dict[str, Any]):
            nonlocal recovered
            for row in current.get("rows", []):
                if str(row.get("status") or "") != "creating" or str(row.get("task_id") or ""):
                    continue
                try:
                    claimed_at = datetime.fromisoformat(str(row.get("claimed_at") or ""))
                    if claimed_at.tzinfo is None:
                        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
                except Exception:
                    claimed_at = cutoff - timedelta(seconds=1)
                if claimed_at <= cutoff:
                    row.update(status="queued", claimed_at="", error="")
                    recovered += 1

        _mutate_job(job_id, mutate)
    return recovered


def claim_next_row(owner_token_hash: str) -> dict[str, Any] | None:
    jobs = [item for item in reversed(list_jobs(owner_token_hash, active_only=True, limit=100)) if str(item.get("status") or "") in {"queued", "running"}]
    for job in jobs:
        job_id = str(job.get("id") or "")

        def mutate(current: dict[str, Any]):
            if str(current.get("status") or "") not in {"queued", "running"} or current.get("canceled_at"):
                return None
            now = datetime.now(timezone.utc)
            row = None
            for item in current.get("rows", []):
                if str(item.get("status") or "") != "queued":
                    continue
                try:
                    due = datetime.fromisoformat(str(item.get("next_attempt_at") or ""))
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                except Exception:
                    due = None
                if not due or due <= now:
                    row = item
                    break
            if not row:
                return None
            row.update(status="creating", claimed_at=_now(), next_attempt_at="", error="")
            current["status"] = "running"
            return {"job": deepcopy(current), "row": deepcopy(row)}

        claimed = _mutate_job(job_id, mutate)
        if claimed:
            return claimed
    return None


def finish_row_creation(job_id: str, row_index: int, task_id: str) -> None:
    def mutate(job: dict[str, Any]):
        row = next((item for item in job.get("rows", []) if int(item.get("index") or 0) == int(row_index)), None)
        if not row:
            raise KeyError(row_index)
        row.update(status="running", task_id=str(task_id), created_at=_now(), claimed_at="", error="")
        job["status"] = "canceling" if job.get("canceled_at") else "running"

    _mutate_job(job_id, mutate)


def fail_or_requeue_row(job_id: str, row_index: int, error: str, *, retry: bool) -> None:
    def mutate(job: dict[str, Any]):
        row = next((item for item in job.get("rows", []) if int(item.get("index") or 0) == int(row_index)), None)
        if not row or str(row.get("status") or "") in TERMINAL_ROW_STATUSES:
            return
        attempts = max(0, int(row.get("create_attempts") or 0)) + 1
        row["create_attempts"] = attempts
        if retry and attempts < 5 and not job.get("canceled_at"):
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(30, 2 ** attempts))
            row.update(status="queued", task_id="", claimed_at="", next_attempt_at=retry_at.isoformat(), error=str(error)[:500])
        else:
            row.update(status="failed", task_id="", claimed_at="", error=str(error)[:500], finished_at=_now())
        _finish_job_if_terminal(job)

    _mutate_job(job_id, mutate)


def reconcile_job(job_id: str, task_payloads: dict[str, dict[str, str]]) -> dict[str, Any]:
    def mutate(job: dict[str, Any]):
        for row in job.get("rows", []):
            task_id = str(row.get("task_id") or "")
            if not task_id or str(row.get("status") or "") in TERMINAL_ROW_STATUSES:
                continue
            task = task_payloads.get(task_id)
            if not task:
                continue
            status = str(task.get("status") or "")
            video_url = str(task.get("video_url") or "")
            if video_url:
                row.update(status="completed", video_url=video_url, error="", finished_at=_now())
            elif status in {"failed", "canceled"}:
                row.update(status="failed" if status == "failed" else "canceled", error=str(task.get("error") or ("用户取消生成" if status == "canceled" else "生成失败"))[:500], finished_at=_now())
            else:
                row["status"] = "running"
        _finish_job_if_terminal(job)
        return job

    return _mutate_job(job_id, mutate)


def _finish_job_if_terminal(job: dict[str, Any]) -> None:
    rows = [item for item in job.get("rows", []) if isinstance(item, dict)]
    if rows and all(str(item.get("status") or "") in TERMINAL_ROW_STATUSES for item in rows):
        job["status"] = "canceled" if job.get("canceled_at") and all(str(item.get("status") or "") == "canceled" for item in rows) else "completed"
        job["finished_at"] = _now()
    elif any(str(item.get("status") or "") in {"creating", "running"} for item in rows):
        job["status"] = "running"
    else:
        job["status"] = "queued"


class BatchCoordinator:
    def __init__(self) -> None:
        queue = get_task_queue()
        self.redis = getattr(queue, "client", None)
        namespace = str(__import__("os").environ.get("DOLA_QUEUE_NAMESPACE") or "dola:tasks").strip().rstrip(":")
        self.owner_list = f"{namespace}:batch:owners"
        self.owner_set = f"{namespace}:batch:owners:known"
        self.scheduler_lock = f"{namespace}:batch:scheduler:lock"

    def register_owner(self, owner: str) -> None:
        if not owner or self.redis is None:
            return
        script = "if redis.call('SADD', KEYS[1], ARGV[1]) == 1 then redis.call('RPUSH', KEYS[2], ARGV[1]) return 1 end return 0"
        self.redis.eval(script, 2, self.owner_set, self.owner_list, owner)

    def next_owner(self, eligible: set[str]) -> str:
        global _LOCAL_OWNER_CURSOR
        normalized = sorted({str(item) for item in eligible if str(item)})
        if not normalized:
            return ""
        if self.redis is None:
            with _LOCK:
                owner = normalized[_LOCAL_OWNER_CURSOR % len(normalized)]
                _LOCAL_OWNER_CURSOR = (_LOCAL_OWNER_CURSOR + 1) % max(1, len(normalized))
                return owner
        for owner in normalized:
            self.register_owner(owner)
        attempts = max(len(normalized), int(self.redis.llen(self.owner_list) or 0))
        for _ in range(max(1, attempts)):
            owner = str(self.redis.lmove(self.owner_list, self.owner_list, "LEFT", "RIGHT") or "")
            if owner in eligible:
                return owner
        return normalized[0]

    @contextmanager
    def lease(self, seconds: int = 10) -> Iterator[bool]:
        if self.redis is None:
            acquired = _SCHEDULER_LOCK.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    _SCHEDULER_LOCK.release()
            return
        token = secrets.token_hex(16)
        acquired = bool(self.redis.set(self.scheduler_lock, token, nx=True, ex=max(3, int(seconds))))
        try:
            yield acquired
        finally:
            if acquired:
                script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
                self.redis.eval(script, 1, self.scheduler_lock, token)


_COORDINATOR: BatchCoordinator | None = None
_COORDINATOR_SIGNATURE = ""


def coordinator() -> BatchCoordinator:
    global _COORDINATOR, _COORDINATOR_SIGNATURE
    queue = get_task_queue()
    signature = f"{getattr(queue, 'backend', 'file')}:{id(queue)}"
    if _COORDINATOR is None or signature != _COORDINATOR_SIGNATURE:
        _COORDINATOR = BatchCoordinator()
        _COORDINATOR_SIGNATURE = signature
    return _COORDINATOR
