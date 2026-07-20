from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any


def queue_backend() -> str:
    backend = str(os.environ.get("DOLA_QUEUE_BACKEND") or "file").strip().lower()
    if backend not in {"file", "redis"}:
        raise RuntimeError("DOLA_QUEUE_BACKEND must be file or redis")
    return backend


class FileTaskQueue:
    backend = "file"

    def enqueue(self, task_id: str, available_at: datetime | None = None) -> bool:
        return True

    def claim(self, worker_id: str, claimed_ids: set[str], active_counts: dict[str, int], concurrency_limits: dict[str, int]) -> str | None:
        from .store import claim_next_pending

        return claim_next_pending(worker_id, claimed_ids, active_counts, concurrency_limits)

    def release(self, task_id: str, worker_id: str = "") -> None:
        return None

    def heartbeat(self, task_ids: set[str] | dict[str, str]) -> None:
        return None

    def recover(self) -> int:
        return 0

    def reconcile(self) -> int:
        return 0

    def health(self) -> dict[str, Any]:
        return {"ok": True, "backend": self.backend}


class RedisTaskQueue:
    backend = "redis"

    def __init__(self) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis queue requires redis; install requirements.txt") from exc
        url = str(os.environ.get("DOLA_REDIS_URL") or "redis://127.0.0.1:6379/0").strip()
        namespace = str(os.environ.get("DOLA_QUEUE_NAMESPACE") or "dola:tasks").strip().rstrip(":")
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        self.ready = f"{namespace}:ready"
        self.processing = f"{namespace}:processing"
        self.delayed = f"{namespace}:delayed"
        self.leases = f"{namespace}:leases"
        self.owners = f"{namespace}:owners"
        self.known = f"{namespace}:known"
        self.visibility_timeout = max(30, int(os.environ.get("DOLA_QUEUE_VISIBILITY_TIMEOUT") or 180))

    def enqueue(self, task_id: str, available_at: datetime | None = None) -> bool:
        score = available_at.timestamp() if available_at else 0
        script = """
        if redis.call('SADD', KEYS[1], ARGV[1]) == 0 then return 0 end
        if tonumber(ARGV[2]) > tonumber(ARGV[3]) then
          redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
        else
          redis.call('LPUSH', KEYS[3], ARGV[1])
        end
        return 1
        """
        return bool(self.client.eval(script, 3, self.known, self.delayed, self.ready, task_id, score, time.time()))

    def _promote(self) -> int:
        now = time.time()
        script = """
        local tasks = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 100)
        for _, task_id in ipairs(tasks) do
          if redis.call('ZREM', KEYS[1], task_id) == 1 then redis.call('LPUSH', KEYS[2], task_id) end
        end
        return #tasks
        """
        return int(self.client.eval(script, 2, self.delayed, self.ready, now))

    def claim(self, worker_id: str, claimed_ids: set[str], active_counts: dict[str, int], concurrency_limits: dict[str, int]) -> str | None:
        from .store import STATUS_PENDING, get_meta, mark_running, parse_time

        self.recover()
        self._promote()
        for _ in range(100):
            task_id = self.client.lmove(self.ready, self.processing, "RIGHT", "LEFT")
            if not task_id:
                return None
            try:
                meta = get_meta(task_id)
            except (FileNotFoundError, ValueError):
                self._ack(task_id)
                continue
            owner = str(meta.get("owner_token_hash") or "")
            if str(meta.get("status") or "") != STATUS_PENDING or bool(meta.get("cancel_requested")):
                self._ack(task_id)
                continue
            next_attempt_at = parse_time(str(meta.get("next_attempt_at") or ""))
            if next_attempt_at and next_attempt_at > datetime.now(timezone.utc):
                self._delay_claimed(task_id, next_attempt_at.timestamp())
                continue
            if not mark_running(task_id, worker_id, concurrency_limits):
                if owner and owner in concurrency_limits:
                    self._delay_claimed(task_id, time.time() + 2)
                else:
                    self._ack(task_id)
                continue
            pipe = self.client.pipeline(transaction=True)
            pipe.hset(self.owners, task_id, worker_id)
            pipe.zadd(self.leases, {task_id: time.time() + self.visibility_timeout})
            pipe.execute()
            return task_id
        return None

    def _delay_claimed(self, task_id: str, score: float) -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.lrem(self.processing, 0, task_id)
        pipe.zadd(self.delayed, {task_id: score})
        pipe.zrem(self.leases, task_id)
        pipe.hdel(self.owners, task_id)
        pipe.execute()

    def _ack(self, task_id: str) -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.lrem(self.ready, 0, task_id)
        pipe.lrem(self.processing, 0, task_id)
        pipe.zrem(self.leases, task_id)
        pipe.zrem(self.delayed, task_id)
        pipe.hdel(self.owners, task_id)
        pipe.srem(self.known, task_id)
        pipe.execute()

    def release(self, task_id: str, worker_id: str = "") -> None:
        from .store import STATUS_PENDING, get_meta, parse_time

        owner = str(worker_id or "")
        try:
            meta = get_meta(task_id)
        except FileNotFoundError:
            self._ack(task_id) if not owner else self._ack_owned(task_id, owner)
            return
        if str(meta.get("status") or "") == STATUS_PENDING and not bool(meta.get("cancel_requested")):
            available_at = parse_time(str(meta.get("next_attempt_at") or ""))
            score = (available_at or datetime.now(timezone.utc)).timestamp()
            self._delay_claimed(task_id, score) if not owner else self._delay_owned(task_id, owner, score)
        else:
            self._ack(task_id) if not owner else self._ack_owned(task_id, owner)

    def heartbeat(self, task_ids: set[str] | dict[str, str]) -> None:
        if not task_ids:
            return
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        if redis.call('ZSCORE', KEYS[2], ARGV[1]) == false then return 0 end
        redis.call('ZADD', KEYS[2], ARGV[3], ARGV[1])
        return 1
        """
        leases = task_ids if isinstance(task_ids, dict) else {task_id: "" for task_id in task_ids}
        for task_id, worker_id in leases.items():
            self.client.eval(script, 2, self.owners, self.leases, task_id, worker_id, time.time() + self.visibility_timeout)

    def _delay_owned(self, task_id: str, worker_id: str, score: float) -> None:
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        redis.call('LREM', KEYS[3], 0, ARGV[1])
        redis.call('ZADD', KEYS[4], ARGV[3], ARGV[1])
        redis.call('ZREM', KEYS[2], ARGV[1])
        redis.call('HDEL', KEYS[1], ARGV[1])
        return 1
        """
        self.client.eval(script, 4, self.owners, self.leases, self.processing, self.delayed, task_id, worker_id, score)

    def _ack_owned(self, task_id: str, worker_id: str) -> None:
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        redis.call('LREM', KEYS[2], 0, ARGV[1])
        redis.call('ZREM', KEYS[3], ARGV[1])
        redis.call('ZREM', KEYS[4], ARGV[1])
        redis.call('HDEL', KEYS[1], ARGV[1])
        redis.call('SREM', KEYS[5], ARGV[1])
        return 1
        """
        self.client.eval(script, 5, self.owners, self.processing, self.leases, self.delayed, self.known, task_id, worker_id)

    def recover(self) -> int:
        expired = self.client.zrangebyscore(self.leases, "-inf", time.time(), start=0, num=100)
        if not expired:
            return 0
        from .store import STATUS_RUNNING, get_meta, load_result, mark_pending, mark_submitted

        recovered = 0
        script = """
        local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
        if not score or tonumber(score) > tonumber(ARGV[2]) then return 0 end
        redis.call('ZREM', KEYS[1], ARGV[1])
        redis.call('HDEL', KEYS[2], ARGV[1])
        redis.call('LREM', KEYS[3], 0, ARGV[1])
        return 1
        """
        for task_id in expired:
            if not self.client.eval(script, 3, self.leases, self.owners, self.processing, task_id, time.time()):
                continue
            recovered += 1
            try:
                meta = get_meta(task_id)
                if str(meta.get("status") or "") == STATUS_RUNNING:
                    result = load_result(task_id)
                    submitted = bool(result.get("conversation_id") or result.get("doubao_submit_confirmed") or result.get("qianwen_submit_confirmed") or result.get("qianwen_remote_task_ids"))
                    if submitted:
                        mark_submitted(task_id)
                    else:
                        mark_pending(task_id, "worker lease expired")
                        self.client.lpush(self.ready, task_id)
                elif str(meta.get("status") or "") == "pending":
                    self.client.lpush(self.ready, task_id)
                else:
                    self.client.srem(self.known, task_id)
            except FileNotFoundError:
                self.client.srem(self.known, task_id)
        return recovered

    def reconcile(self) -> int:
        from .store import STATUS_PENDING, get_meta, list_tasks, parse_time

        added = 0
        for item in list_tasks():
            if str(item.get("status") or "") != STATUS_PENDING:
                continue
            meta = get_meta(str(item["id"]))
            available_at = parse_time(str(meta.get("next_attempt_at") or ""))
            added += int(self.enqueue(str(item["id"]), available_at))
        return added

    def health(self) -> dict[str, Any]:
        try:
            self.client.ping()
            return {"ok": True, "backend": self.backend, "ready": self.client.llen(self.ready), "processing": self.client.llen(self.processing), "delayed": self.client.zcard(self.delayed)}
        except Exception as exc:
            return {"ok": False, "backend": self.backend, "error": str(exc)[:200]}


_queue: FileTaskQueue | RedisTaskQueue | None = None
_queue_signature = ""


def get_task_queue() -> FileTaskQueue | RedisTaskQueue:
    global _queue, _queue_signature
    signature = "|".join([queue_backend(), str(os.environ.get("DOLA_REDIS_URL") or ""), str(os.environ.get("DOLA_QUEUE_NAMESPACE") or "")])
    if _queue is None or signature != _queue_signature:
        _queue = RedisTaskQueue() if queue_backend() == "redis" else FileTaskQueue()
        _queue_signature = signature
    return _queue
