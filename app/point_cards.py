from __future__ import annotations

import hashlib
import json
import secrets
import string
import threading
from datetime import datetime, timezone
from typing import Any

from . import postgres
from .billing import points_to_units, units_to_points
from .config import DATA_DIR, ensure_dirs
from .temp_access import add_temp_credit_units


POINT_CARDS_PATH = DATA_DIR / "point_cards.json"
_LOCK = threading.RLock()
_ALPHABET = string.ascii_uppercase + string.digits


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(code: str) -> str:
    return "".join(character for character in str(code or "").upper() if character.isalnum())


def _digest(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("ascii")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "HS-" + "-".join(raw[index:index + 4] for index in range(0, 16, 4))


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        return postgres.read_document("point_cards", {"cards": {}})
    if not POINT_CARDS_PATH.exists():
        return {"cards": {}}
    data = json.loads(POINT_CARDS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) and isinstance(data.get("cards"), dict) else {"cards": {}}


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("point_cards", data)
        return
    temporary = POINT_CARDS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(POINT_CARDS_PATH)


def generate_cards(points: object, count: int, note: str = "") -> list[dict[str, Any]]:
    units = points_to_units(points)
    count = int(count)
    if count < 1 or count > 200:
        raise ValueError("生成数量需为 1-200")
    created: list[dict[str, Any]] = []
    with _LOCK:
        data = _read()
        for _ in range(count):
            code = _new_code()
            digest = _digest(code)
            now = _now()
            record = {
                "id": secrets.token_hex(12),
                "code_hash": digest,
                "code_hint": f"{code[:7]}...{code[-4:]}",
                "points_units": units,
                "points": units_to_points(units),
                "status": "unused",
                "note": str(note or "").strip()[:120],
                "created_at": now,
                "redeemed_at": "",
                "redeemed_by": "",
            }
            data["cards"][digest] = record
            created.append({**record, "code": code})
        _write(data)
    return created


def list_cards(limit: int = 500) -> list[dict[str, Any]]:
    with _LOCK:
        rows = [dict(item) for item in _read()["cards"].values() if isinstance(item, dict)]
    rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return rows[:max(1, min(2000, int(limit)))]


def redeem_card(code: str, user_id: str, token_hash: str) -> dict[str, Any]:
    normalized = _normalize(code)
    if len(normalized) < 12:
        raise ValueError("卡密格式无效")
    digest = _digest(normalized)
    with _LOCK:
        data = _read()
        card = data["cards"].get(digest)
        if not isinstance(card, dict):
            raise KeyError("card not found")
        if card.get("status") != "unused":
            raise ValueError("卡密已被使用")
        card["status"] = "redeeming"
        card["redeemed_by"] = str(user_id or "")
        _write(data)
        try:
            credited = add_temp_credit_units(token_hash, int(card.get("points_units") or 0))
        except Exception:
            card["status"] = "unused"
            card["redeemed_by"] = ""
            _write(data)
            raise
        card["status"] = "redeemed"
        card["redeemed_at"] = _now()
        _write(data)
        return {"card": dict(card), "balance": credited}
