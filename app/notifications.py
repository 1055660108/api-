from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from . import postgres
from .config import DATA_DIR, ensure_dirs


NOTIFICATIONS_PATH = DATA_DIR / "notifications.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        return postgres.read_document("notifications", {"notifications": {}})
    if not NOTIFICATIONS_PATH.exists():
        return {"notifications": {}}
    data = json.loads(NOTIFICATIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), dict):
        return {"notifications": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("notifications", data)
        return
    temporary = NOTIFICATIONS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(NOTIFICATIONS_PATH)


def create_notifications(recipients: list[dict[str, Any]], title: str, content: str) -> dict[str, Any]:
    title = str(title or "").strip()
    content = str(content or "").strip()
    if not title or len(title) > 120:
        raise ValueError("通知标题需为 1-120 个字符")
    if not content or len(content) > 5000:
        raise ValueError("通知内容需为 1-5000 个字符")
    unique_recipients: dict[str, dict[str, Any]] = {}
    for recipient in recipients:
        user_id = str(recipient.get("id") or "").strip()
        if user_id:
            unique_recipients[user_id] = recipient
    if not unique_recipients:
        raise ValueError("请至少选择一位用户")

    now = _now()
    batch_id = secrets.token_hex(12)
    created: list[dict[str, Any]] = []
    with _LOCK:
        data = _read()
        for user_id, recipient in unique_recipients.items():
            notification_id = secrets.token_hex(12)
            record = {
                "id": notification_id,
                "batch_id": batch_id,
                "user_id": user_id,
                "username": str(recipient.get("username") or ""),
                "title": title,
                "content": content,
                "read_at": "",
                "created_at": now,
            }
            data["notifications"][notification_id] = record
            created.append(dict(record))
        _write(data)
    return {"batch_id": batch_id, "recipient_count": len(created), "notifications": created}


def list_notifications_for_user(user_id: str) -> list[dict[str, Any]]:
    user_id = str(user_id or "").strip()
    with _LOCK:
        rows = [
            dict(item)
            for item in _read()["notifications"].values()
            if isinstance(item, dict) and str(item.get("user_id") or "") == user_id
        ]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows


def list_admin_notifications(limit: int = 200) -> list[dict[str, Any]]:
    with _LOCK:
        rows = [dict(item) for item in _read()["notifications"].values() if isinstance(item, dict)]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[:max(1, min(1000, int(limit)))]


def mark_notification_read(notification_id: str, user_id: str) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        record = data["notifications"].get(str(notification_id or ""))
        if not isinstance(record, dict) or str(record.get("user_id") or "") != str(user_id or ""):
            raise KeyError(notification_id)
        if not record.get("read_at"):
            record["read_at"] = _now()
            _write(data)
        return dict(record)
