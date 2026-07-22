from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from . import postgres
from .billing import nonnegative_points_to_units, points_to_units, units_to_points
from .config import DATA_DIR, ensure_dirs


MEMBERSHIP_PATH = DATA_DIR / "membership_packages.json"
DEFAULT_PAYMENT_URL = "https://pay.ldxp.cn/shop/huisu/fhm9gj"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        return postgres.read_document("membership_packages", {"packages": []})
    if not MEMBERSHIP_PATH.exists():
        return {"packages": []}
    data = json.loads(MEMBERSHIP_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) and isinstance(data.get("packages"), list) else {"packages": []}


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("membership_packages", data)
        return
    temporary = MEMBERSHIP_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MEMBERSHIP_PATH)


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("会员套餐名称不能为空")
    raw_points_cost = item.get("points_cost")
    if raw_points_cost is None:
        raw_points_cost = item.get("price") or 1
    points_cost = units_to_points(points_to_units(raw_points_cost))
    duration_days = int(item.get("duration_days") or 0)
    concurrency = int(item.get("concurrency") or 1)
    bonus_free_uses = int(item.get("bonus_free_uses") or 0)
    raw_task_discount = item.get("task_discount_points")
    task_discount_points = units_to_points(nonnegative_points_to_units(0.1 if raw_task_discount is None else raw_task_discount))
    if duration_days < 1 or duration_days > 3650:
        raise ValueError("会员积分或有效期无效")
    if concurrency < 1 or concurrency > 100:
        raise ValueError("会员并发数量需为 1-100")
    if bonus_free_uses < 0 or bonus_free_uses > 1000000:
        raise ValueError("赠送视频额度需为 0-1000000")
    return {
        "id": str(item.get("id") or secrets.token_hex(8)),
        "name": name,
        "points_cost": points_cost,
        "duration_days": duration_days,
        "concurrency": concurrency,
        "bonus_free_uses": bonus_free_uses,
        "task_discount_points": task_discount_points,
        "description": str(item.get("description") or "").strip()[:500],
        "payment_url": str(item.get("payment_url") or DEFAULT_PAYMENT_URL).strip()[:500],
        "enabled": bool(item.get("enabled", True)),
        "sort_order": int(item.get("sort_order") or 0),
        "created_at": str(item.get("created_at") or _now()),
        "updated_at": str(item.get("updated_at") or _now()),
    }


def list_memberships(include_disabled: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        rows = [_normalize(item) for item in _read()["packages"] if isinstance(item, dict)]
    if not include_disabled:
        rows = [item for item in rows if item["enabled"]]
    return sorted(rows, key=lambda item: (item["sort_order"], item["created_at"]))


def get_membership(package_id: str) -> dict[str, Any]:
    item = next((row for row in list_memberships() if str(row.get("id") or "") == str(package_id or "")), None)
    if not isinstance(item, dict):
        raise KeyError(package_id)
    return item


def create_membership(payload: dict[str, Any]) -> dict[str, Any]:
    item = _normalize(payload)
    with _LOCK:
        data = _read()
        data["packages"].append(item)
        _write(data)
    return dict(item)


def update_membership(package_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        item = next((row for row in data["packages"] if str(row.get("id")) == str(package_id)), None)
        if not isinstance(item, dict):
            raise KeyError(package_id)
        normalized = _normalize({**item, **payload, "id": package_id, "updated_at": _now()})
        item.clear()
        item.update(normalized)
        _write(data)
        return dict(item)


def disable_membership(package_id: str) -> dict[str, Any]:
    return update_membership(package_id, {"enabled": False})
