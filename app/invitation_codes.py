from __future__ import annotations

import hashlib
import json
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from . import postgres
from .config import DATA_DIR, ensure_dirs


INVITATION_CODES_PATH = DATA_DIR / "invitation_codes.json"
_ALPHABET = string.ascii_uppercase + string.digits
_LOCK = threading.RLock()
_MAX_USE_HISTORY = 100
T = TypeVar("T")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(code: object) -> str:
    return "".join(character for character in str(code or "").upper() if character.isalnum())


def _digest(code: object) -> str:
    return hashlib.sha256(_normalize(code).encode("ascii", errors="ignore")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "HSI-" + "-".join(raw[index:index + 4] for index in range(0, 12, 4))


def _default() -> dict[str, Any]:
    return {"required": True, "codes": {}}


def _normalize_data(data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("required", True)
    codes = data.setdefault("codes", {})
    if not isinstance(codes, dict):
        raise RuntimeError("invitation code data is corrupt")
    for code_hash, record in list(codes.items()):
        if not isinstance(record, dict):
            del codes[code_hash]
            continue
        record.setdefault("id", secrets.token_hex(12))
        record.setdefault("code_hash", str(code_hash))
        record.setdefault("code", "")
        record.setdefault("created_at", "")
        # Codes created by the earlier one-time implementation remain active.
        previous_uses = 1 if str(record.get("status") or "") == "used" else 0
        record["use_count"] = max(0, int(record.get("use_count") or previous_uses))
        record.setdefault("last_used_at", str(record.get("used_at") or ""))
        record.setdefault("last_used_by", str(record.get("used_by") or ""))
        record.setdefault("last_used_username", str(record.get("used_username") or ""))
        uses = record.get("uses")
        record["uses"] = uses[-_MAX_USE_HISTORY:] if isinstance(uses, list) else []
        for obsolete in ("status", "reserved_at", "reservation_id", "used_at", "used_by", "used_username"):
            record.pop(obsolete, None)
    return data


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        data = postgres.read_document("invitation_codes", _default())
    elif INVITATION_CODES_PATH.exists():
        try:
            data = json.loads(INVITATION_CODES_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invitation code data is corrupt: {INVITATION_CODES_PATH}") from exc
    else:
        data = _default()
    if not isinstance(data, dict):
        raise RuntimeError("invitation code data is corrupt")
    return _normalize_data(data)


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("invitation_codes", data)
        return
    ensure_dirs()
    temporary = Path(INVITATION_CODES_PATH).with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(INVITATION_CODES_PATH)


def _mutate(mutator: Callable[[dict[str, Any]], T]) -> T:
    def normalized_mutator(data: dict[str, Any]) -> T:
        return mutator(_normalize_data(data))

    if postgres.enabled():
        return postgres.mutate_document("invitation_codes", _default(), normalized_mutator)
    with _LOCK:
        data = _read()
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        result = normalized_mutator(data)
        if json.dumps(data, ensure_ascii=False, sort_keys=True) != before:
            _write(data)
        return result


def registration_required() -> bool:
    with _LOCK:
        return bool(_read().get("required", True))


def set_registration_required(required: bool) -> dict[str, Any]:
    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        data["required"] = bool(required)
        data["updated_at"] = _now()
        return invitation_state(data)

    return _mutate(mutate)


def generate_codes(count: int) -> list[dict[str, Any]]:
    count = int(count)
    if count < 1 or count > 200:
        raise ValueError("生成数量需为 1-200")

    def mutate(data: dict[str, Any]) -> list[dict[str, Any]]:
        codes = data.setdefault("codes", {})
        created = []
        for _ in range(count):
            code = _new_code()
            while _digest(code) in codes:
                code = _new_code()
            now = _now()
            record = {
                "id": secrets.token_hex(12),
                "code": code,
                "code_hash": _digest(code),
                "created_at": now,
                "use_count": 0,
                "last_used_at": "",
                "last_used_by": "",
                "last_used_username": "",
                "uses": [],
            }
            codes[record["code_hash"]] = record
            created.append(dict(record))
        data["updated_at"] = _now()
        return created

    return _mutate(mutate)


def reserve_code(code: object) -> dict[str, str]:
    normalized = _normalize(code)
    if len(normalized) < 8:
        raise ValueError("邀请码无效")
    code_hash = _digest(normalized)

    def validate(data: dict[str, Any]) -> dict[str, str]:
        record = data.get("codes", {}).get(code_hash)
        if not isinstance(record, dict):
            raise ValueError("邀请码无效")
        return {"reservation_id": code_hash, "code": str(record.get("code") or "")}

    return _mutate(validate)


def complete_reservation(reservation_id: str, user_id: str, username: str) -> None:
    code_hash = str(reservation_id or "")

    def mutate(data: dict[str, Any]) -> None:
        record = data.get("codes", {}).get(code_hash)
        if not isinstance(record, dict):
            # A registration validated immediately before a manual deletion may finish.
            return
        used_at = _now()
        use = {
            "used_at": used_at,
            "user_id": str(user_id or ""),
            "username": str(username or "")[:80],
        }
        record["use_count"] = max(0, int(record.get("use_count") or 0)) + 1
        record["last_used_at"] = used_at
        record["last_used_by"] = use["user_id"]
        record["last_used_username"] = use["username"]
        history = record.setdefault("uses", [])
        history.append(use)
        del history[:-_MAX_USE_HISTORY]
        data["updated_at"] = used_at

    _mutate(mutate)


def release_reservation(reservation_id: str) -> None:
    # Reusable codes are never locked while a registration is in progress.
    return None


def delete_code(code_id: object) -> bool:
    normalized_id = str(code_id or "").strip()
    if not normalized_id:
        return False

    def mutate(data: dict[str, Any]) -> bool:
        codes = data.get("codes", {})
        code_hash = next(
            (
                key
                for key, record in codes.items()
                if isinstance(record, dict) and str(record.get("id") or "") == normalized_id
            ),
            "",
        )
        if not code_hash:
            return False
        del codes[code_hash]
        data["updated_at"] = _now()
        return True

    return _mutate(mutate)


def invitation_state(data: dict[str, Any] | None = None, limit: int = 200) -> dict[str, Any]:
    source = _normalize_data(data) if isinstance(data, dict) else _read()
    rows = [dict(item) for item in source.get("codes", {}).values() if isinstance(item, dict)]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "required": bool(source.get("required", True)),
        "counts": {
            "total": len(rows),
            "uses": sum(max(0, int(item.get("use_count") or 0)) for item in rows),
        },
        "codes": rows[:max(1, min(1000, int(limit)))],
    }
