from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .billing import package_bonus_free_uses, points_to_units, units_to_points
from .config import DATA_DIR, ensure_dirs
from . import postgres


PACKAGE_CATALOG_PATH = DATA_DIR / "point_packages.json"
DEFAULT_PACKAGE_POINTS = (1, 6, 18, 30, 68, 128, 256)
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_packages() -> list[dict[str, Any]]:
    timestamp = _now()
    return [
        {
            "id": f"points-{points}",
            "name": f"{points} 积分",
            "points": points,
            "bonus_free_uses": package_bonus_free_uses(points),
            "enabled": True,
            "sort_order": index,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        for index, points in enumerate(DEFAULT_PACKAGE_POINTS, 1)
    ]


def _normalize_package(item: dict[str, Any]) -> dict[str, Any]:
    package_id = str(item.get("id") or "").strip()
    if not package_id:
        raise ValueError("套餐 ID 不能为空")
    points = units_to_points(points_to_units(item.get("points")))
    name = str(item.get("name") or f"{points} 积分").strip()[:80]
    if not name:
        raise ValueError("套餐名称不能为空")
    bonus = int(item.get("bonus_free_uses", package_bonus_free_uses(points)))
    if bonus < 0 or bonus > 1000000:
        raise ValueError("赠送次数需为 0-1000000")
    return {
        "id": package_id,
        "name": name,
        "points": points,
        "bonus_free_uses": bonus,
        "enabled": bool(item.get("enabled", True)),
        "sort_order": int(item.get("sort_order", 0)),
        "created_at": str(item.get("created_at") or _now()),
        "updated_at": str(item.get("updated_at") or _now()),
    }


def _read() -> dict[str, list[dict[str, Any]]]:
    ensure_dirs()
    if postgres.enabled():
        loaded = postgres.read_document("point_packages")
        if not loaded:
            loaded = {"packages": _default_packages()}
            _write(loaded)
        if not isinstance(loaded.get("packages"), list):
            raise RuntimeError("package catalog is corrupt: PostgreSQL point_packages document")
        return {"packages": [_normalize_package(item) for item in loaded["packages"] if isinstance(item, dict)]}
    if not PACKAGE_CATALOG_PATH.exists():
        data = {"packages": _default_packages()}
        _write(data)
        return data
    try:
        loaded = json.loads(PACKAGE_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"package catalog is corrupt: {PACKAGE_CATALOG_PATH}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("packages"), list):
        raise RuntimeError(f"package catalog is corrupt: {PACKAGE_CATALOG_PATH}")
    return {"packages": [_normalize_package(item) for item in loaded["packages"] if isinstance(item, dict)]}


def _write(data: dict[str, list[dict[str, Any]]]) -> None:
    if postgres.enabled():
        postgres.write_document("point_packages", data)
        return
    PACKAGE_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = PACKAGE_CATALOG_PATH.with_name(f"{PACKAGE_CATALOG_PATH.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(PACKAGE_CATALOG_PATH)
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def list_packages(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        packages = _read()["packages"]
        if not include_disabled:
            packages = [item for item in packages if item["enabled"]]
        return sorted((dict(item) for item in packages), key=lambda item: (item["sort_order"], item["created_at"], item["id"]))


def create_package(payload: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        package_id = str(payload.get("id") or secrets.token_hex(8)).strip()
        if any(item["id"] == package_id for item in data["packages"]):
            raise ValueError("套餐 ID 已存在")
        timestamp = _now()
        item = _normalize_package({**payload, "id": package_id, "enabled": payload.get("enabled", True), "created_at": timestamp, "updated_at": timestamp})
        data["packages"].append(item)
        _write(data)
        return dict(item)


def update_package(package_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "points", "bonus_free_uses", "enabled", "sort_order"}
    updates = {key: value for key, value in payload.items() if key in allowed}
    if not updates:
        raise ValueError("没有可更新的套餐字段")
    with _LOCK:
        data = _read()
        item = next((entry for entry in data["packages"] if entry["id"] == package_id), None)
        if item is None:
            raise KeyError(package_id)
        normalized = _normalize_package({**item, **updates, "updated_at": _now()})
        item.clear()
        item.update(normalized)
        _write(data)
        return dict(item)


def disable_package(package_id: str) -> dict[str, Any]:
    return update_package(package_id, {"enabled": False})
