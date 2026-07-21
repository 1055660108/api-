from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from . import postgres
from .billing import POINT_SCALE
from .config import DATA_DIR, ensure_dirs


TRANSACTIONS_PATH = DATA_DIR / "point_transactions.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _points(units: int) -> int | float:
    value = int(units)
    return value // POINT_SCALE if value % POINT_SCALE == 0 else value / POINT_SCALE


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        return postgres.read_document("point_transactions", {"transactions": []})
    if not TRANSACTIONS_PATH.exists():
        return {"transactions": []}
    data = json.loads(TRANSACTIONS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) and isinstance(data.get("transactions"), list) else {"transactions": []}


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("point_transactions", data)
        return
    temporary = TRANSACTIONS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(TRANSACTIONS_PATH)


def record_transaction(
    user_id: str,
    kind: str,
    amount_units: int,
    title: str,
    *,
    balance_units: int | None = None,
    reference_id: str = "",
    detail: str = "",
    video_quota_change: int = 0,
    video_quota_balance: int | None = None,
) -> dict[str, Any]:
    user_id = str(user_id or "").strip()
    if not user_id:
        return {}
    entry = {
        "id": secrets.token_hex(12),
        "user_id": user_id,
        "kind": str(kind or "adjustment")[:40],
        "amount_units": int(amount_units),
        "amount": _points(amount_units),
        "balance_units": int(balance_units) if balance_units is not None else None,
        "balance": _points(balance_units) if balance_units is not None else None,
        "video_quota_change": int(video_quota_change),
        "video_quota_balance": max(0, int(video_quota_balance)) if video_quota_balance is not None else None,
        "title": str(title or "积分变动").strip()[:120],
        "detail": str(detail or "").strip()[:500],
        "reference_id": str(reference_id or "").strip()[:120],
        "created_at": _now(),
    }
    with _LOCK:
        data = _read()
        data["transactions"].append(entry)
        data["transactions"] = data["transactions"][-10000:]
        _write(data)
    return dict(entry)


def list_transactions(user_id: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    with _LOCK:
        rows = [dict(item) for item in _read()["transactions"] if isinstance(item, dict) and str(item.get("user_id") or "") == str(user_id or "")]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    page_size = max(1, min(100, int(page_size)))
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), total_pages)
    start = (page - 1) * page_size
    return {"transactions": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": total_pages}
