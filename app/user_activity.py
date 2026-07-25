from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from . import config, postgres


_LOCK = threading.RLock()
_MAX_LOCAL_ENTRIES = 20_000


def _path():
    return config.DATA_DIR / "user_activity.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = payload.get("activities", []) if isinstance(payload, dict) else []
    return [dict(item) for item in rows if isinstance(item, dict)]


def _write(rows: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"activities": rows[-_MAX_LOCAL_ENTRIES:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def record_activity(
    user_id: str,
    action: str,
    title: str,
    *,
    detail: str = "",
    reference_id: str = "",
    actor: str = "user",
) -> dict[str, Any]:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return {}
    entry = {
        "id": secrets.token_hex(12),
        "user_id": normalized_user_id[:64],
        "action": str(action or "operation").strip()[:60],
        "title": str(title or "用户操作").strip()[:120],
        "detail": str(detail or "").strip()[:1000],
        "reference_id": str(reference_id or "").strip()[:120],
        "actor": str(actor or "user").strip()[:40],
        "created_at": _now(),
    }
    with _LOCK:
        if postgres.enabled():
            postgres.insert_user_activity(entry)
        else:
            rows = _read()
            rows.append(entry)
            _write(rows)
    return dict(entry)


def list_activity(user_id: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page_size = max(1, min(100, int(page_size)))
    if postgres.enabled():
        return postgres.query_user_activity(str(user_id or ""), page, page_size)
    with _LOCK:
        rows = [item for item in _read() if str(item.get("user_id") or "") == str(user_id or "")]
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(max(1, int(page)), total_pages)
    start = (current_page - 1) * page_size
    return {"activities": rows[start:start + page_size], "total": total, "page": current_page, "page_size": page_size, "total_pages": total_pages}
