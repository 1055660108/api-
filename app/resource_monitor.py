from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from typing import Any

from . import config, postgres
from .resilience import memory_pressure
from .task_queue import get_task_queue


logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_LAST_LEVEL = "pending"
_SNAPSHOT: dict[str, Any] = {
    "level": "pending",
    "alerts": [],
    "updated_at": "",
    "disk": {},
    "memory": {},
    "redis": {},
    "postgres": {},
}


def _level(ratio: float, warning: float, critical: float) -> str:
    if ratio >= critical:
        return "critical"
    if ratio >= warning:
        return "warning"
    return "normal"


def _worker_memory() -> dict[str, Any]:
    path = config.DATA_DIR / ".worker-health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        resources = payload.get("resources") if isinstance(payload.get("resources"), dict) else {}
        ratio = max(0.0, float(resources.get("memory_ratio") or 0.0))
        return {
            "ok": bool(payload.get("ok")),
            "ratio": round(ratio, 4),
            "used_bytes": max(0, int(resources.get("memory_used_bytes") or 0)),
            "limit_bytes": max(0, int(resources.get("memory_limit_bytes") or 0)),
            "level": _level(ratio, 0.80, 0.90),
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"ok": False, "ratio": 0.0, "used_bytes": 0, "limit_bytes": 0, "level": "unavailable"}


def collect_resource_snapshot() -> dict[str, Any]:
    alerts: list[dict[str, str]] = []
    disk_usage = shutil.disk_usage(config.DATA_DIR)
    disk_ratio = disk_usage.used / disk_usage.total if disk_usage.total else 0.0
    disk_level = _level(disk_ratio, 0.75, 0.85)
    disk = {
        "level": disk_level,
        "ratio": round(disk_ratio, 4),
        "total_bytes": int(disk_usage.total),
        "used_bytes": int(disk_usage.used),
        "free_bytes": int(disk_usage.free),
    }
    if disk_level != "normal":
        alerts.append({"id": "disk", "level": disk_level, "message": f"磁盘使用率 {disk_ratio:.0%}"})

    api_ratio, api_used, api_limit = memory_pressure()
    api_level = _level(api_ratio, 0.80, 0.90)
    worker = _worker_memory()
    memory_level = "critical" if "critical" in {api_level, worker["level"]} else "warning" if "warning" in {api_level, worker["level"]} else "normal"
    memory = {
        "level": memory_level,
        "api": {"level": api_level, "ratio": round(api_ratio, 4), "used_bytes": api_used, "limit_bytes": api_limit},
        "worker": worker,
    }
    if api_level != "normal":
        alerts.append({"id": "api_memory", "level": api_level, "message": f"API 容器内存 {api_ratio:.0%}"})
    if worker["level"] in {"warning", "critical"}:
        alerts.append({"id": "worker_memory", "level": str(worker["level"]), "message": f"Worker 容器内存 {float(worker['ratio']):.0%}"})

    queue = get_task_queue()
    redis_snapshot: dict[str, Any]
    if getattr(queue, "backend", "file") != "redis" or getattr(queue, "client", None) is None:
        redis_snapshot = {"configured": False, "ok": True, "level": "normal"}
    else:
        try:
            info = queue.client.info("memory")
            clients = queue.client.info("clients")
            used = max(0, int(info.get("used_memory") or 0))
            maximum = max(0, int(info.get("maxmemory") or 0))
            ratio = used / maximum if maximum else 0.0
            if maximum:
                redis_level = _level(ratio, 0.75, 0.90)
            else:
                warning_bytes = max(64 * 1024 * 1024, int(os.environ.get("DOLA_REDIS_MEMORY_WARNING_BYTES") or 512 * 1024 * 1024))
                redis_level = "critical" if used >= warning_bytes * 2 else "warning" if used >= warning_bytes else "normal"
            redis_snapshot = {
                "configured": True,
                "ok": True,
                "level": redis_level,
                "used_memory_bytes": used,
                "maxmemory_bytes": maximum,
                "memory_ratio": round(ratio, 4),
                "connected_clients": max(0, int(clients.get("connected_clients") or 0)),
            }
            if redis_level != "normal":
                alerts.append({"id": "redis_memory", "level": redis_level, "message": f"Redis 内存已使用 {used / 1024 / 1024:.0f} MB"})
        except Exception as exc:
            redis_snapshot = {"configured": True, "ok": False, "level": "critical", "error": str(exc)[:200]}
            alerts.append({"id": "redis", "level": "critical", "message": "Redis 连接异常"})

    if not postgres.enabled():
        postgres_snapshot = {"configured": False, "ok": True, "level": "normal"}
    else:
        try:
            postgres_snapshot = postgres.health_snapshot()
            maximum = max(1, int(postgres_snapshot.get("max_connections") or 1))
            ratio = max(0.0, int(postgres_snapshot.get("connections") or 0) / maximum)
            postgres_level = _level(ratio, 0.70, 0.85)
            postgres_snapshot.update(level=postgres_level, connection_ratio=round(ratio, 4), configured=True)
            if postgres_level != "normal":
                alerts.append({"id": "postgres_connections", "level": postgres_level, "message": f"PostgreSQL 连接使用率 {ratio:.0%}"})
        except Exception as exc:
            postgres_snapshot = {"configured": True, "ok": False, "level": "critical", "error": str(exc)[:200]}
            alerts.append({"id": "postgres", "level": "critical", "message": "PostgreSQL 连接异常"})

    overall = "critical" if any(item["level"] == "critical" for item in alerts) else "warning" if alerts else "normal"
    snapshot = {
        "level": overall,
        "alerts": alerts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "disk": disk,
        "memory": memory,
        "redis": redis_snapshot,
        "postgres": postgres_snapshot,
    }
    global _SNAPSHOT, _LAST_LEVEL
    with _LOCK:
        if overall != _LAST_LEVEL and overall in {"warning", "critical"}:
            logger.warning("resource alert level changed to %s: %s", overall, "; ".join(item["message"] for item in alerts))
        _LAST_LEVEL = overall
        _SNAPSHOT = snapshot
    return snapshot


def latest_resource_snapshot() -> dict[str, Any]:
    with _LOCK:
        return json.loads(json.dumps(_SNAPSHOT, ensure_ascii=False))
