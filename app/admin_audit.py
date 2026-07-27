from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import postgres
from .config import DATA_DIR, ensure_dirs


ADMIN_AUDIT_PATH = DATA_DIR / "admin_audit.json"
_LOCK = threading.RLock()
_MAX_ENTRIES = 10_000


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
        del entries[:-_MAX_ENTRIES]

    with _LOCK:
        if postgres.enabled():
            postgres.mutate_document("admin_audit", _default(), append)
        else:
            payload = _read()
            append(payload)
            _write(payload)
    return dict(entry)


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
