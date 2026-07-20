from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from . import postgres
from .config import DATA_DIR, ensure_dirs


FEEDBACK_PATH = DATA_DIR / "feedback.json"
_LOCK = threading.RLock()
STATUSES = {"pending", "reviewing", "resolved", "closed"}
CATEGORIES = {"体验建议", "问题反馈", "其他"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        return postgres.read_document("feedback", {"feedback": {}})
    if not FEEDBACK_PATH.exists():
        return {"feedback": {}}
    data = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) and isinstance(data.get("feedback"), dict) else {"feedback": {}}


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("feedback", data)
        return
    temporary = FEEDBACK_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(FEEDBACK_PATH)


def create_feedback(user: dict[str, Any], category: str, content: str, contact: str = "", source_page: str = "") -> dict[str, Any]:
    category = str(category or "其他").strip()
    content = str(content or "").strip()
    if category not in CATEGORIES:
        raise ValueError("反馈类型无效")
    if len(content) < 2 or len(content) > 5000:
        raise ValueError("反馈内容需为2-5000字")
    now = _now()
    record = {
        "id": secrets.token_hex(12),
        "user_id": str(user.get("id") or ""),
        "username": str(user.get("username") or ""),
        "email": str(user.get("email") or ""),
        "category": category,
        "content": content,
        "contact": str(contact or "").strip()[:254],
        "source_page": str(source_page or "").strip()[:200],
        "status": "pending",
        "admin_note": "",
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        data = _read()
        data["feedback"][record["id"]] = record
        _write(data)
    return record


def list_feedback(page: int = 1, page_size: int = 20, status: str = "", query: str = "") -> dict[str, Any]:
    with _LOCK:
        rows = list(_read()["feedback"].values())
    query = str(query or "").strip().casefold()
    rows = [item for item in rows if isinstance(item, dict) and (not status or item.get("status") == status) and (not query or query in str(item.get("content") or "").casefold() or query in str(item.get("username") or "").casefold())]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return {"feedback": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": total_pages}


def update_feedback(feedback_id: str, status: str, admin_note: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError("反馈状态无效")
    with _LOCK:
        data = _read()
        record = data["feedback"].get(feedback_id)
        if not isinstance(record, dict):
            raise KeyError(feedback_id)
        record["status"] = status
        record["admin_note"] = str(admin_note or "").strip()[:5000]
        record["updated_at"] = _now()
        if status in {"resolved", "closed"}:
            record["resolved_at"] = record["updated_at"]
        _write(data)
        return record
