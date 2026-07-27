from __future__ import annotations

import hashlib
import threading
import time
from typing import Any


FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 10 * 60
BLOCK_SECONDS = 15 * 60
_LOCK = threading.RLock()
_LOCAL_STATE: dict[str, dict[str, float | int]] = {}
_MAX_LOCAL_KEYS = 10_000

_FAILURE_SCRIPT = """
local failures = KEYS[1]
local blocked = KEYS[2]
local threshold = tonumber(ARGV[1])
local failure_window = tonumber(ARGV[2])
local block_seconds = tonumber(ARGV[3])
local blocked_ttl = redis.call('TTL', blocked)
if blocked_ttl > 0 then return {threshold, blocked_ttl, 0} end
local count = redis.call('INCR', failures)
if count == 1 then redis.call('EXPIRE', failures, failure_window) end
if count >= threshold then
  redis.call('SET', blocked, '1', 'EX', block_seconds)
  redis.call('DEL', failures)
  return {count, block_seconds, 1}
end
return {count, 0, 0}
"""


def _redis_client():
    try:
        from .task_queue import get_task_queue

        return getattr(get_task_queue(), "client", None)
    except Exception:
        return None


def _redis_keys(client_key: str) -> tuple[str, str]:
    digest = hashlib.sha256(str(client_key or "unknown").encode("utf-8", errors="replace")).hexdigest()
    return f"dola:registration-failures:{digest}", f"dola:registration-block:{digest}"


def _redis_block_retry(client_key: str) -> int | None:
    try:
        client = _redis_client()
        if client is None:
            return None
        _failures, blocked = _redis_keys(client_key)
        return max(0, int(client.ttl(blocked)))
    except Exception:
        return None


def _local_state(client_key: str, now: float) -> dict[str, float | int]:
    with _LOCK:
        state = _LOCAL_STATE.setdefault(client_key, {"failures": 0, "last_failure": 0.0, "blocked_until": 0.0})
        if float(state.get("blocked_until") or 0) <= now:
            state["blocked_until"] = 0.0
        if now - float(state.get("last_failure") or 0) >= FAILURE_WINDOW_SECONDS:
            state["failures"] = 0
        return state


def block_retry_after(client_key: str) -> int:
    redis_retry = _redis_block_retry(client_key)
    if redis_retry is not None:
        return redis_retry
    now = time.monotonic()
    state = _local_state(client_key, now)
    return max(0, int(float(state.get("blocked_until") or 0) - now + 0.999))


def record_failure(client_key: str) -> dict[str, Any]:
    try:
        client = _redis_client()
        if client is not None:
            failures, blocked = _redis_keys(client_key)
            result = client.eval(
                _FAILURE_SCRIPT,
                2,
                failures,
                blocked,
                FAILURE_THRESHOLD,
                FAILURE_WINDOW_SECONDS,
                BLOCK_SECONDS,
            )
            return {"failures": int(result[0]), "retry_after": int(result[1]), "blocked_now": bool(int(result[2]))}
    except Exception:
        pass

    now = time.monotonic()
    with _LOCK:
        state = _local_state(client_key, now)
        retry_after = max(0, int(float(state.get("blocked_until") or 0) - now + 0.999))
        if retry_after:
            return {"failures": int(state.get("failures") or FAILURE_THRESHOLD), "retry_after": retry_after, "blocked_now": False}
        state["failures"] = int(state.get("failures") or 0) + 1
        state["last_failure"] = now
        blocked_now = int(state["failures"]) >= FAILURE_THRESHOLD
        if blocked_now:
            state["blocked_until"] = now + BLOCK_SECONDS
            state["failures"] = 0
        if len(_LOCAL_STATE) > _MAX_LOCAL_KEYS:
            for key, item in list(_LOCAL_STATE.items()):
                if now - float(item.get("last_failure") or 0) >= FAILURE_WINDOW_SECONDS and float(item.get("blocked_until") or 0) <= now:
                    _LOCAL_STATE.pop(key, None)
            while len(_LOCAL_STATE) > _MAX_LOCAL_KEYS:
                _LOCAL_STATE.pop(next(iter(_LOCAL_STATE)))
        return {
            "failures": FAILURE_THRESHOLD if blocked_now else int(state["failures"]),
            "retry_after": BLOCK_SECONDS if blocked_now else 0,
            "blocked_now": blocked_now,
        }


def reset_failures(client_key: str) -> None:
    try:
        client = _redis_client()
        if client is not None:
            client.delete(*_redis_keys(client_key))
            return
    except Exception:
        pass
    with _LOCK:
        _LOCAL_STATE.pop(client_key, None)


def clear_local_state() -> None:
    with _LOCK:
        _LOCAL_STATE.clear()
