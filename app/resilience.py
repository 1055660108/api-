from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ResiliencePolicy:
    platform_rate_per_minute: int
    platform_burst: int
    circuit_failure_threshold: int
    circuit_recovery_seconds: int
    queue_high_watermark: int
    memory_high_ratio: float
    memory_critical_ratio: float
    minimum_workers: int


def load_policy() -> ResiliencePolicy:
    high = min(0.98, _env_float("DOLA_MEMORY_HIGH_RATIO", 0.80))
    critical = min(0.99, max(high + 0.01, _env_float("DOLA_MEMORY_CRITICAL_RATIO", 0.92)))
    return ResiliencePolicy(
        platform_rate_per_minute=_env_int("DOLA_PLATFORM_RATE_PER_MINUTE", 30),
        platform_burst=_env_int("DOLA_PLATFORM_BURST", 5),
        circuit_failure_threshold=_env_int("DOLA_CIRCUIT_FAILURE_THRESHOLD", 5),
        circuit_recovery_seconds=_env_int("DOLA_CIRCUIT_RECOVERY_SECONDS", 60),
        queue_high_watermark=_env_int("DOLA_QUEUE_HIGH_WATERMARK", 1000),
        memory_high_ratio=high,
        memory_critical_ratio=critical,
        minimum_workers=_env_int("DOLA_MINIMUM_WORKERS", 1),
    )


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str = ""
    retry_after: int = 0


_LOCAL_LOCK = threading.RLock()
_LOCAL_BUCKETS: dict[str, tuple[float, float]] = {}
_LOCAL_CIRCUITS: dict[str, dict[str, float]] = {}


class PlatformGuard:
    def __init__(self, redis_client: Any | None = None, namespace: str | None = None) -> None:
        self.redis = redis_client
        self.namespace = (namespace or os.environ.get("DOLA_QUEUE_NAMESPACE") or "dola:tasks").strip().rstrip(":")

    def admit(self, platform: str) -> Admission:
        policy = load_policy()
        circuit = self._admit_circuit(platform, policy)
        if not circuit.allowed:
            return circuit
        limited = self._take_token(platform, policy)
        if not limited.allowed and circuit.reason == "half_open":
            self._release_probe(platform)
        return limited

    def record_success(self, platform: str) -> None:
        if self.redis is not None:
            self.redis.delete(self._key(platform, "failures"), self._key(platform, "opened"), self._key(platform, "probe"))
            return
        with _LOCAL_LOCK:
            _LOCAL_CIRCUITS.pop(self._local_key(platform), None)

    def record_failure(self, platform: str) -> None:
        policy = load_policy()
        if self.redis is not None:
            script = """
            local failures = redis.call('INCR', KEYS[1])
            redis.call('EXPIRE', KEYS[1], ARGV[1])
            if failures >= tonumber(ARGV[2]) then
              redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[1])
              redis.call('DEL', KEYS[3])
            end
            return failures
            """
            self.redis.eval(
                script,
                3,
                self._key(platform, "failures"),
                self._key(platform, "opened"),
                self._key(platform, "probe"),
                policy.circuit_recovery_seconds * 2,
                policy.circuit_failure_threshold,
                time.time(),
            )
            return
        with _LOCAL_LOCK:
            state = _LOCAL_CIRCUITS.setdefault(self._local_key(platform), {"failures": 0.0, "opened": 0.0, "probe": 0.0})
            state["failures"] += 1
            state["probe"] = 0.0
            if state["failures"] >= policy.circuit_failure_threshold:
                state["opened"] = time.time()

    def snapshot(self, platform: str) -> dict[str, Any]:
        policy = load_policy()
        if self.redis is not None:
            failures, opened = self.redis.mget(self._key(platform, "failures"), self._key(platform, "opened"))
            opened_at = float(opened or 0)
            failure_count = int(failures or 0)
        else:
            with _LOCAL_LOCK:
                state = dict(_LOCAL_CIRCUITS.get(self._local_key(platform), {}))
            opened_at = float(state.get("opened") or 0)
            failure_count = int(state.get("failures") or 0)
        retry_after = max(0, math.ceil(policy.circuit_recovery_seconds - (time.time() - opened_at))) if opened_at else 0
        return {"state": "open" if retry_after else "closed", "failures": failure_count, "retry_after": retry_after}

    def _admit_circuit(self, platform: str, policy: ResiliencePolicy) -> Admission:
        now = time.time()
        if self.redis is not None:
            opened_value = self.redis.get(self._key(platform, "opened"))
            if not opened_value:
                return Admission(True)
            elapsed = now - float(opened_value)
            if elapsed < policy.circuit_recovery_seconds:
                return Admission(False, "circuit_open", math.ceil(policy.circuit_recovery_seconds - elapsed))
            acquired = self.redis.set(self._key(platform, "probe"), str(now), nx=True, ex=policy.circuit_recovery_seconds)
            return Admission(bool(acquired), "half_open" if acquired else "circuit_open", policy.circuit_recovery_seconds)
        with _LOCAL_LOCK:
            state = _LOCAL_CIRCUITS.get(self._local_key(platform))
            if not state or not state.get("opened"):
                return Admission(True)
            elapsed = now - state["opened"]
            if elapsed < policy.circuit_recovery_seconds:
                return Admission(False, "circuit_open", math.ceil(policy.circuit_recovery_seconds - elapsed))
            if state.get("probe"):
                return Admission(False, "circuit_open", policy.circuit_recovery_seconds)
            state["probe"] = now
            return Admission(True, "half_open", policy.circuit_recovery_seconds)

    def _take_token(self, platform: str, policy: ResiliencePolicy) -> Admission:
        now = time.time()
        refill = policy.platform_rate_per_minute / 60.0
        if self.redis is not None:
            script = """
            local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
            local tokens = tonumber(values[1]) or tonumber(ARGV[2])
            local updated = tonumber(values[2]) or tonumber(ARGV[1])
            tokens = math.min(tonumber(ARGV[2]), tokens + math.max(0, tonumber(ARGV[1]) - updated) * tonumber(ARGV[3]))
            local allowed = 0
            if tokens >= 1 then tokens = tokens - 1 allowed = 1 end
            redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated', ARGV[1])
            redis.call('EXPIRE', KEYS[1], ARGV[4])
            return {allowed, tokens}
            """
            allowed, tokens = self.redis.eval(script, 1, self._key(platform, "bucket"), now, policy.platform_burst, refill, 120)
            retry_after = 0 if allowed else max(1, math.ceil((1 - float(tokens)) / refill))
            return Admission(bool(allowed), "" if allowed else "rate_limited", retry_after)
        key = self._local_key(platform)
        with _LOCAL_LOCK:
            tokens, updated = _LOCAL_BUCKETS.get(key, (float(policy.platform_burst), now))
            tokens = min(float(policy.platform_burst), tokens + max(0.0, now - updated) * refill)
            if tokens >= 1:
                _LOCAL_BUCKETS[key] = (tokens - 1, now)
                return Admission(True)
            _LOCAL_BUCKETS[key] = (tokens, now)
            return Admission(False, "rate_limited", max(1, math.ceil((1 - tokens) / refill)))

    def _release_probe(self, platform: str) -> None:
        if self.redis is not None:
            self.redis.delete(self._key(platform, "probe"))
            return
        with _LOCAL_LOCK:
            state = _LOCAL_CIRCUITS.get(self._local_key(platform))
            if state:
                state["probe"] = 0.0

    def _key(self, platform: str, suffix: str) -> str:
        return f"{self.namespace}:resilience:{platform}:{suffix}"

    def _local_key(self, platform: str) -> str:
        return f"{self.namespace}:{platform}"


def memory_pressure() -> tuple[float, int, int]:
    candidates = (
        (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory.max")),
        (Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")),
    )
    for usage_path, limit_path in candidates:
        try:
            usage = int(usage_path.read_text(encoding="utf-8").strip())
            raw_limit = limit_path.read_text(encoding="utf-8").strip()
            if raw_limit != "max":
                limit = int(raw_limit)
                if 0 < limit < 1 << 60:
                    return usage / limit, usage, limit
        except (OSError, ValueError):
            pass
    if os.name != "nt":
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
                name, _, value = line.partition(":")
                if name in {"MemTotal", "MemAvailable"}:
                    values[name] = int(value.strip().split()[0]) * 1024
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", 0)
            if total:
                return (total - available) / total, total - available, total
        except (OSError, ValueError):
            pass
    return 0.0, 0, 0


def adaptive_worker_limit(configured: int) -> tuple[int, dict[str, Any]]:
    policy = load_policy()
    ratio, used, limit = memory_pressure()
    capacity = _env_int("DOLA_MAX_EFFECTIVE_WORKERS", 8)
    capped = min(configured, capacity)
    minimum = min(capped, policy.minimum_workers)
    if ratio >= policy.memory_critical_ratio:
        effective = minimum
        level = "critical"
    elif ratio >= policy.memory_high_ratio:
        span = policy.memory_critical_ratio - policy.memory_high_ratio
        remaining = max(0.0, (policy.memory_critical_ratio - ratio) / span)
        effective = max(minimum, math.floor(capped * max(0.25, remaining)))
        level = "high"
    else:
        effective = capped
        level = "normal"
    return effective, {"level": level, "memory_ratio": round(ratio, 4), "memory_used_bytes": used, "memory_limit_bytes": limit, "configured_workers": configured, "capacity_limit": capacity, "effective_workers": effective}


def queue_admission(queue_health: dict[str, Any]) -> Admission:
    policy = load_policy()
    depth = sum(max(0, int(queue_health.get(key) or 0)) for key in ("ready", "processing", "delayed"))
    if depth >= policy.queue_high_watermark:
        return Admission(False, "queue_overloaded", 10)
    return Admission(True)
