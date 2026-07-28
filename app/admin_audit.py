from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import postgres
from .config import DATA_DIR, ensure_dirs


ADMIN_AUDIT_PATH = DATA_DIR / "admin_audit.json"
_LOCK = threading.RLock()
_MAX_ENTRIES = 10_000
_RETENTION_DAYS = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default() -> dict[str, Any]:
    return {"entries": []}


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        payload = postgres.read_document("admin_audit", _default())
    elif ADMIN_AUDIT_PATH.exists():
        try:
            payload = json.loads(ADMIN_AUDIT_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = _default()
    else:
        payload = _default()
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return _default()
    return payload


def _write(payload: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("admin_audit", payload)
        return
    ensure_dirs()
    temporary = Path(ADMIN_AUDIT_PATH).with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(ADMIN_AUDIT_PATH)


def record_admin_action(
    action: str,
    title: str,
    *,
    detail: str = "",
    actor: str = "管理员",
    ip_address: str = "",
    reference_id: str = "",
) -> dict[str, Any]:
    entry = {
        "id": secrets.token_hex(12),
        "action": str(action or "operation").strip()[:60],
        "title": str(title or "管理操作").strip()[:120],
        "detail": str(detail or "").strip()[:2000],
        "actor": str(actor or "管理员").strip()[:80],
        "ip_address": str(ip_address or "").strip()[:80],
        "reference_id": str(reference_id or "").strip()[:120],
        "created_at": _now(),
    }

    def append(payload: dict[str, Any]) -> None:
        entries = payload.setdefault("entries", [])
        entries.append(entry)
        payload["entries"] = _retained_entries(entries, _RETENTION_DAYS, _MAX_ENTRIES)

    with _LOCK:
        if postgres.enabled():
            postgres.mutate_document("admin_audit", _default(), append)
        else:
            payload = _read()
            append(payload)
            _write(payload)
    return dict(entry)


def _retained_entries(entries: list[Any], retention_days: int, max_entries: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))
    retained: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        try:
            created_at = datetime.fromisoformat(str(raw.get("created_at") or "").replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if created_at >= cutoff:
            retained.append(dict(raw))
    return retained[-max(1, int(max_entries)):]


def prune_admin_actions(retention_days: int = _RETENTION_DAYS, max_entries: int = _MAX_ENTRIES) -> int:
    removed = 0

    def prune(payload: dict[str, Any]) -> None:
        nonlocal removed
        entries = payload.setdefault("entries", [])
        retained = _retained_entries(entries, retention_days, max_entries)
        removed = max(0, len(entries) - len(retained))
        payload["entries"] = retained

    with _LOCK:
        if postgres.enabled():
            postgres.mutate_document("admin_audit", _default(), prune)
        else:
            payload = _read()
            prune(payload)
            if removed:
                _write(payload)
    return removed


def list_admin_actions(
    page: int = 1,
    page_size: int = 20,
    query: str = "",
    action: str = "all",
) -> dict[str, Any]:
    normalized_query = str(query or "").strip().lower()
    normalized_action = str(action or "all").strip().lower()
    with _LOCK:
        rows = [dict(item) for item in _read().get("entries", []) if isinstance(item, dict)]
    if normalized_action not in {"", "all"}:
        rows = [item for item in rows if str(item.get("action") or "").lower() == normalized_action]
    if normalized_query:
        rows = [
            item
            for item in rows
            if normalized_query in " ".join(
                str(item.get(key) or "").lower()
                for key in ("title", "detail", "actor", "ip_address", "reference_id")
            )
        ]
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    total = len(rows)
    normalized_page_size = max(10, min(100, int(page_size)))
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    current_page = min(max(1, int(page)), total_pages)
    start = (current_page - 1) * normalized_page_size
    return {
        "entries": rows[start:start + normalized_page_size],
        "total": total,
        "page": current_page,
        "page_size": normalized_page_size,
        "total_pages": total_pages,
    }
