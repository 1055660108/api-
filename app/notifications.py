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


def _empty() -> dict[str, Any]:
    return {"notifications": {}, "announcements": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        data = postgres.read_document("notifications", _empty())
        data.setdefault("notifications", {})
        data.setdefault("announcements", {})
        return data
    if not NOTIFICATIONS_PATH.exists():
        return _empty()
    data = json.loads(NOTIFICATIONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), dict):
        return _empty()
    data.setdefault("announcements", {})
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


def mark_all_notifications_read(user_id: str) -> int:
    changed = 0
    with _LOCK:
        data = _read()
        now = _now()
        for record in data["notifications"].values():
            if isinstance(record, dict) and str(record.get("user_id") or "") == str(user_id or "") and not record.get("read_at"):
                record["read_at"] = now
                changed += 1
        if changed:
            _write(data)
    return changed


def create_announcement(title: str, content: str) -> dict[str, Any]:
    title = str(title or "").strip()
    content = str(content or "").strip()
    if not title or len(title) > 120:
        raise ValueError("公告标题需为 1-120 个字符")
    if not content or len(content) > 5000:
        raise ValueError("公告内容需为 1-5000 个字符")
    now = _now()
    announcement = {
        "id": secrets.token_hex(12),
        "title": title,
        "content": content,
        "enabled": True,
        "seen_by": [],
        "created_at": now,
        "updated_at": now,
    }
    with _LOCK:
        data = _read()
        data["announcements"][announcement["id"]] = announcement
        _write(data)
    return dict(announcement)


def list_announcements(user_id: str = "", include_disabled: bool = False) -> list[dict[str, Any]]:
    with _LOCK:
        rows = [dict(item) for item in _read()["announcements"].values() if isinstance(item, dict)]
    if not include_disabled:
        rows = [item for item in rows if item.get("enabled", True)]
    for item in rows:
        seen_by = {str(value) for value in item.pop("seen_by", [])}
        item["seen"] = bool(user_id and str(user_id) in seen_by)
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows


def mark_announcement_seen(announcement_id: str, user_id: str) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        record = data["announcements"].get(str(announcement_id or ""))
        if not isinstance(record, dict) or not record.get("enabled", True):
            raise KeyError(announcement_id)
        seen_by = [str(item) for item in record.get("seen_by") or []]
        if str(user_id or "") not in seen_by:
            seen_by.append(str(user_id or ""))
            record["seen_by"] = seen_by[-100000:]
            record["updated_at"] = _now()
            _write(data)
        public = dict(record)
        public.pop("seen_by", None)
        public["seen"] = True
        return public


def set_announcement_enabled(announcement_id: str, enabled: bool) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        record = data["announcements"].get(str(announcement_id or ""))
        if not isinstance(record, dict):
            raise KeyError(announcement_id)
        record["enabled"] = bool(enabled)
        record["updated_at"] = _now()
        _write(data)
        public = dict(record)
        public.pop("seen_by", None)
        return public
