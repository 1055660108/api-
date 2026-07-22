from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .billing import POINT_SCALE, points_to_units, units_to_points
from .config import DATA_DIR, ensure_dirs
from . import postgres


TEMP_TOKEN_COUNT = 20
TEMP_TOKEN_LIMIT = 100
TEMP_TOKEN_CONCURRENCY = 1
MAX_TEMP_TOKEN_CONCURRENCY = 100
DEFAULT_TASK_RETENTION_DAYS = 7
MIN_TASK_RETENTION_DAYS = 1
MAX_TASK_RETENTION_DAYS = 15
VIDEO_FIRST = "video_first"
POINTS_FIRST = "points_first"
BILLING_PRIORITIES = {VIDEO_FIRST, POINTS_FIRST}
TEMP_TOKENS_PATH = DATA_DIR / "temp_tokens.json"
_LOCK = threading.Lock()


class QuotaExceeded(Exception):
    pass


@dataclass(frozen=True)
class AccessContext:
    token_hash: str
    is_admin: bool
    is_temp: bool
    limit: int = 0
    used: int = 0
    remaining: int = 0
    concurrency: int = 0
    task_retention_days: int = DEFAULT_TASK_RETENTION_DAYS
    free_remaining: int = 0
    credit_units: int = 0
    billing_priority: str = VIDEO_FIRST


def normalize_billing_priority(value: Any) -> str:
    priority = str(value or VIDEO_FIRST).strip().lower()
    if priority not in BILLING_PRIORITIES:
        raise ValueError("扣费优先级无效")
    return priority


def normalize_retention_days(value: Any) -> int:
    return max(MIN_TASK_RETENTION_DAYS, min(MAX_TASK_RETENTION_DAYS, int(value or DEFAULT_TASK_RETENTION_DAYS)))


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_data() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        loaded = postgres.read_document("temp_tokens", {"tokens": {}})
        tokens = loaded.get("tokens")
        return {"tokens": tokens if isinstance(tokens, dict) else {}}
    if not TEMP_TOKENS_PATH.exists():
        return {"tokens": {}}
    try:
        loaded = json.loads(TEMP_TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"temporary token data is corrupt: {TEMP_TOKENS_PATH}") from exc
    if not isinstance(loaded, dict):
        return {"tokens": {}}
    tokens = loaded.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    return {"tokens": tokens}


def _write_data(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("temp_tokens", data)
        return
    TEMP_TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TEMP_TOKENS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TEMP_TOKENS_PATH)


def _read_token_entry(token_hash: str) -> dict[str, Any] | None:
    normalized_hash = str(token_hash or "").strip().lower()
    if not normalized_hash:
        return None
    if postgres.enabled():
        return postgres.read_temp_token(normalized_hash)
    entry = _read_data()["tokens"].get(normalized_hash)
    return dict(entry) if isinstance(entry, dict) else None


def _public_token(token_hash: str, entry: dict[str, Any]) -> dict[str, Any]:
    limit = max(0, int(entry.get("limit") or TEMP_TOKEN_LIMIT))
    used = max(0, int(entry.get("used") or 0))
    concurrency = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(entry.get("concurrency") or TEMP_TOKEN_CONCURRENCY)))
    task_retention_days = normalize_retention_days(entry.get("task_retention_days"))
    remark = str(entry.get("remark") or entry.get("note") or "")[:100]
    free_remaining = max(0, int(entry.get("free_remaining") or 0))
    credit_units = max(0, int(entry.get("credit_units") or 0))
    billing_priority = normalize_billing_priority(entry.get("billing_priority"))
    return {
        "id": token_hash,
        "token": str(entry.get("token") or ""),
        "remark": remark,
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "free_remaining": free_remaining,
        "credit_units": credit_units,
        "points": units_to_points(credit_units),
        "billing_priority": billing_priority,
        "concurrency": concurrency,
        "task_retention_days": task_retention_days,
        "created_at": str(entry.get("created_at") or ""),
        "updated_at": str(entry.get("updated_at") or ""),
    }


def list_temp_tokens() -> list[dict[str, Any]]:
    data = _read_data()
    items = [_public_token(token_hash, entry) for token_hash, entry in data["tokens"].items() if isinstance(entry, dict)]
    return sorted(items, key=lambda item: item.get("created_at") or "")


def temp_token_remarks() -> dict[str, str]:
    data = _read_data()
    remarks: dict[str, str] = {}
    for token_hash, entry in data["tokens"].items():
        if isinstance(entry, dict):
            remarks[str(token_hash)] = str(entry.get("remark") or entry.get("note") or "")[:100]
    return remarks


def temp_token_retention_days() -> dict[str, int]:
    data = _read_data()
    values: dict[str, int] = {}
    for token_hash, entry in data["tokens"].items():
        if isinstance(entry, dict):
            values[str(token_hash)] = normalize_retention_days(entry.get("task_retention_days"))
    return values


def create_temp_tokens(count: int, limit: int = TEMP_TOKEN_LIMIT, concurrency: int = TEMP_TOKEN_CONCURRENCY, remark: str = "", task_retention_days: int = DEFAULT_TASK_RETENTION_DAYS) -> list[dict[str, Any]]:
    count = max(1, min(200, int(count)))
    limit = max(1, min(100000, int(limit)))
    concurrency = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(concurrency)))
    task_retention_days = normalize_retention_days(task_retention_days)
    remark = str(remark or "")[:100]
    created: list[dict[str, Any]] = []
    with _LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> list[dict[str, Any]]:
                tokens = data.setdefault("tokens", {})
                while len(created) < count:
                    token = f"tmp_{secrets.token_urlsafe(24)}"
                    token_hash = hash_token(token)
                    if token_hash in tokens:
                        continue
                    entry = {"token": token, "limit": limit, "concurrency": concurrency, "task_retention_days": task_retention_days, "remark": remark, "used": 0, "free_remaining": limit, "credit_units": 0, "billing_version": 2, "reservations": {}, "created_at": _now()}
                    tokens[token_hash] = entry
                    created.append(_public_token(token_hash, entry))
                return created

            return postgres.mutate_document("temp_tokens", {"tokens": {}}, mutate)
        data = _read_data()
        tokens = data["tokens"]
        while len(created) < count:
            token = f"tmp_{secrets.token_urlsafe(24)}"
            token_hash = hash_token(token)
            if token_hash in tokens:
                continue
            entry = {
                "token": token,
                "limit": limit,
                "concurrency": concurrency,
                "task_retention_days": task_retention_days,
                "remark": remark,
                "used": 0,
                "free_remaining": limit,
                "credit_units": 0,
                "billing_version": 2,
                "reservations": {},
                "created_at": _now(),
            }
            tokens[token_hash] = entry
            created.append(_public_token(token_hash, entry))
        _write_data(data)
    return created


def ensure_temp_tokens(count: int = TEMP_TOKEN_COUNT, limit: int = TEMP_TOKEN_LIMIT) -> list[str]:
    data = _read_data()
    current = len(data["tokens"])
    if current < count:
        create_temp_tokens(count - current, limit)
        data = _read_data()
    ordered = sorted(data["tokens"].values(), key=lambda item: str(item.get("created_at") or ""))
    return [str(item.get("token") or "") for item in ordered if item.get("token")]


def update_temp_token(token_hash: str, *, limit: int | None = None, concurrency: int | None = None, remark: str | None = None, task_retention_days: int | None = None, billing_priority: str | None = None) -> dict[str, Any]:
    token_hash = str(token_hash or "").strip().lower()
    with _LOCK:
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> dict[str, Any]:
                if limit is not None:
                    entry["limit"] = max(1, min(100000, int(limit)))
                if concurrency is not None:
                    entry["concurrency"] = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(concurrency)))
                if task_retention_days is not None:
                    entry["task_retention_days"] = normalize_retention_days(task_retention_days)
                if remark is not None:
                    entry["remark"] = str(remark or "")[:100]
                if billing_priority is not None:
                    entry["billing_priority"] = normalize_billing_priority(billing_priority)
                entry["updated_at"] = _now()
                return _public_token(token_hash, entry)

            return postgres.mutate_temp_token(token_hash, mutate)
        data = _read_data()
        entry = data["tokens"].get(token_hash)
        if not isinstance(entry, dict):
            raise KeyError("token not found")
        if limit is not None:
            entry["limit"] = max(1, min(100000, int(limit)))
        if concurrency is not None:
            entry["concurrency"] = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(concurrency)))
        if task_retention_days is not None:
            entry["task_retention_days"] = normalize_retention_days(task_retention_days)
        if remark is not None:
            entry["remark"] = str(remark or "")[:100]
        if billing_priority is not None:
            entry["billing_priority"] = normalize_billing_priority(billing_priority)
        entry["updated_at"] = _now()
        _write_data(data)
        return _public_token(token_hash, entry)


def deduct_temp_points(token_hash: str, free_limit: int, amount: object) -> dict[str, Any]:
    token_hash = str(token_hash or "").strip().lower()
    units = points_to_units(amount)
    with _LOCK:
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> dict[str, Any]:
                _migrate_entry(entry, free_limit)
                if int(entry.get("credit_units") or 0) < units:
                    raise ValueError("用户积分不足")
                entry["credit_units"] = int(entry.get("credit_units") or 0) - units
                _sync_legacy_fields(entry)
                entry["updated_at"] = _now()
                return _public_token(token_hash, entry)

            return postgres.mutate_temp_token(token_hash, mutate)
        data = _read_data()
        entry = data["tokens"].get(token_hash)
        if not isinstance(entry, dict):
            raise KeyError("token not found")
        _migrate_entry(entry, free_limit)
        if int(entry.get("credit_units") or 0) < units:
            raise ValueError("用户积分不足")
        entry["credit_units"] = int(entry.get("credit_units") or 0) - units
        _sync_legacy_fields(entry)
        entry["updated_at"] = _now()
        _write_data(data)
        return _public_token(token_hash, entry)


def purchase_temp_membership(token_hash: str, free_limit: int, points_cost: object, bonus_free_uses: int, concurrency: int) -> dict[str, Any]:
    token_hash = str(token_hash or "").strip().lower()
    units = points_to_units(points_cost)
    bonus = max(0, int(bonus_free_uses))
    target_concurrency = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(concurrency)))
    with _LOCK:
        def apply(entry: dict[str, Any]) -> dict[str, Any]:
            _migrate_entry(entry, free_limit)
            if int(entry.get("credit_units") or 0) < units:
                raise ValueError("用户积分不足")
            entry["credit_units"] = int(entry.get("credit_units") or 0) - units
            entry["free_remaining"] = int(entry.get("free_remaining") or 0) + bonus
            entry["concurrency"] = target_concurrency
            _sync_legacy_fields(entry)
            entry["updated_at"] = _now()
            return _public_token(token_hash, entry)

        if postgres.enabled():
            return postgres.mutate_temp_token(token_hash, apply)
        data = _read_data()
        entry = data["tokens"].get(token_hash)
        if not isinstance(entry, dict):
            raise KeyError("token not found")
        result = apply(entry)
        _write_data(data)
        return result


def temp_token_concurrency_limits() -> dict[str, int]:
    data = _read_data()
    limits: dict[str, int] = {}
    for token_hash, entry in data["tokens"].items():
        if isinstance(entry, dict):
            limits[str(token_hash)] = max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(entry.get("concurrency") or TEMP_TOKEN_CONCURRENCY)))
    return limits


def delete_temp_token(token_hash: str) -> bool:
    token_hash = str(token_hash or "").strip().lower()
    with _LOCK:
        if postgres.enabled():
            return postgres.delete_temp_token(token_hash)
        data = _read_data()
        existed = token_hash in data["tokens"]
        data["tokens"].pop(token_hash, None)
        _write_data(data)
        return existed


def rotate_temp_token(token_hash: str) -> dict[str, Any]:
    token_hash = str(token_hash or "").strip().lower()
    with _LOCK:
        if postgres.enabled():
            while True:
                token = f"tmp_{secrets.token_urlsafe(24)}"
                new_hash = hash_token(token)
                replacement = postgres.rotate_temp_token(token_hash, new_hash, token, _now())
                if replacement is not None:
                    return _public_token(new_hash, replacement)
        data = _read_data()
        entry = data["tokens"].get(token_hash)
        if not isinstance(entry, dict):
            raise KeyError("token not found")
        while True:
            token = f"tmp_{secrets.token_urlsafe(24)}"
            new_hash = hash_token(token)
            if new_hash not in data["tokens"]:
                break
        replacement = dict(entry)
        replacement["token"] = token
        replacement["updated_at"] = _now()
        data["tokens"][new_hash] = replacement
        data["tokens"].pop(token_hash, None)
        _write_data(data)
        return _public_token(new_hash, replacement)


def get_temp_context(token: str) -> AccessContext | None:
    if not token:
        return None
    token_hash = hash_token(token)
    entry = _read_token_entry(token_hash)
    if not isinstance(entry, dict):
        return None
    limit = max(0, int(entry.get("limit") or TEMP_TOKEN_LIMIT))
    used = max(0, int(entry.get("used") or 0))
    return AccessContext(
        token_hash=token_hash,
        is_admin=False,
        is_temp=True,
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        concurrency=max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(entry.get("concurrency") or TEMP_TOKEN_CONCURRENCY))),
        task_retention_days=normalize_retention_days(entry.get("task_retention_days")),
        free_remaining=max(0, int(entry.get("free_remaining") or 0)),
        credit_units=max(0, int(entry.get("credit_units") or 0)),
        billing_priority=normalize_billing_priority(entry.get("billing_priority")),
    )


def get_temp_context_by_hash(token_hash: str) -> AccessContext | None:
    normalized_hash = str(token_hash or "").strip().lower()
    entry = _read_token_entry(normalized_hash)
    return get_temp_context_from_entry(normalized_hash, entry) if isinstance(entry, dict) else None


def get_temp_reservation(token_hash: str, task_id: str) -> dict[str, Any]:
    entry = _read_token_entry(token_hash)
    if not isinstance(entry, dict):
        return {}
    reservation = entry.get("reservations", {}).get(str(task_id or ""))
    return dict(reservation) if isinstance(reservation, dict) else {}


def _migrate_entry(entry: dict[str, Any], free_limit: int) -> bool:
    if int(entry.get("billing_version") or 0) >= 2:
        return False
    free_limit = max(0, int(free_limit or 0))
    limit = max(free_limit, int(entry.get("limit") or free_limit))
    used = max(0, int(entry.get("used") or 0))
    entry["free_remaining"] = max(0, free_limit - used)
    entry["credit_units"] = max(0, limit - max(used, free_limit)) * POINT_SCALE
    entry["billing_version"] = 2
    entry["reservations"] = {}
    _sync_legacy_fields(entry)
    return True


def _sync_legacy_fields(entry: dict[str, Any]) -> None:
    free_remaining = max(0, int(entry.get("free_remaining") or 0))
    credit_units = max(0, int(entry.get("credit_units") or 0))
    entry["limit"] = free_remaining + (credit_units + POINT_SCALE - 1) // POINT_SCALE
    entry["used"] = 0


def _prune_reservations(entry: dict[str, Any], max_closed: int = 1000) -> None:
    reservations = entry.get("reservations")
    if not isinstance(reservations, dict) or len(reservations) <= max_closed:
        return
    active: dict[str, Any] = {}
    closed: list[tuple[str, dict[str, Any]]] = []
    for task_id, reservation in reservations.items():
        if not isinstance(reservation, dict):
            continue
        if str(reservation.get("status") or "reserved") == "reserved":
            active[str(task_id)] = reservation
        else:
            closed.append((str(task_id), reservation))
    closed.sort(
        key=lambda item: str(item[1].get("refunded_at") or item[1].get("created_at") or ""),
        reverse=True,
    )
    entry["reservations"] = {**active, **dict(closed[:max_closed])}


def migrate_temp_token(token_hash: str, free_limit: int) -> bool:
    with _LOCK:
        normalized_hash = str(token_hash or "").strip().lower()
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> bool:
                changed = _migrate_entry(entry, free_limit)
                if changed:
                    entry["updated_at"] = _now()
                return changed

            try:
                return postgres.mutate_temp_token(normalized_hash, mutate)
            except KeyError:
                return False
        data = _read_data()
        entry = data["tokens"].get(normalized_hash)
        if not isinstance(entry, dict):
            return False
        changed = _migrate_entry(entry, free_limit)
        if changed:
            entry["updated_at"] = _now()
            _write_data(data)
        return changed


def add_temp_credit_units(token_hash: str, units: int) -> dict[str, Any]:
    if int(units) <= 0:
        raise ValueError("积分必须大于0")
    with _LOCK:
        normalized_hash = str(token_hash or "").strip().lower()
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> dict[str, Any]:
                _migrate_entry(entry, 0)
                entry["credit_units"] = int(entry.get("credit_units") or 0) + int(units)
                _sync_legacy_fields(entry)
                entry["updated_at"] = _now()
                return _public_token(normalized_hash, entry)

            return postgres.mutate_temp_token(normalized_hash, mutate)
        data = _read_data()
        entry = data["tokens"].get(normalized_hash)
        if not isinstance(entry, dict):
            raise KeyError("token not found")
        _migrate_entry(entry, 0)
        entry["credit_units"] = int(entry.get("credit_units") or 0) + int(units)
        _sync_legacy_fields(entry)
        entry["updated_at"] = _now()
        _write_data(data)
        return _public_token(normalized_hash, entry)


def reserve_temp_quota(access: AccessContext, task_id: str = "", cost_units: int = POINT_SCALE, user_id: str = "") -> AccessContext:
    if not access.is_temp:
        return access
    with _LOCK:
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> AccessContext:
                _migrate_entry(entry, access.free_remaining)
                reservations = entry.setdefault("reservations", {})
                if task_id and isinstance(reservations.get(task_id), dict):
                    reservation = reservations[task_id]
                    if reservation.get("status") == "reserved":
                        return get_temp_context_from_entry(access.token_hash, entry)
                    raise QuotaExceeded("task quota reservation is closed")
                required_units = max(1, int(cost_units))
                points_available = int(entry.get("credit_units") or 0) >= required_units
                free_used = int(entry.get("free_remaining") or 0) > 0 and not (
                    normalize_billing_priority(entry.get("billing_priority")) == POINTS_FIRST and points_available
                )
                charged_units = 0 if free_used else required_units
                if not free_used and int(entry.get("credit_units") or 0) < charged_units:
                    raise QuotaExceeded("temporary token quota exhausted")
                if free_used:
                    entry["free_remaining"] = int(entry.get("free_remaining") or 0) - 1
                else:
                    entry["credit_units"] = int(entry.get("credit_units") or 0) - charged_units
                if task_id:
                    reservations[task_id] = {"status": "reserved", "free": free_used, "units": charged_units, "user_id": str(user_id or ""), "created_at": _now()}
                _prune_reservations(entry)
                _sync_legacy_fields(entry)
                entry["updated_at"] = _now()
                return get_temp_context_from_entry(access.token_hash, entry)

            try:
                return postgres.mutate_temp_token(access.token_hash, mutate)
            except KeyError as exc:
                raise QuotaExceeded("temporary token is invalid") from exc
        data = _read_data()
        entry = data["tokens"].get(access.token_hash)
        if not isinstance(entry, dict):
            raise QuotaExceeded("temporary token is invalid")
        _migrate_entry(entry, access.free_remaining)
        reservations = entry.setdefault("reservations", {})
        if task_id and isinstance(reservations.get(task_id), dict):
            reservation = reservations[task_id]
            if reservation.get("status") == "reserved":
                return get_temp_context_from_entry(access.token_hash, entry)
            raise QuotaExceeded("task quota reservation is closed")
        required_units = max(1, int(cost_units))
        points_available = int(entry.get("credit_units") or 0) >= required_units
        free_used = int(entry.get("free_remaining") or 0) > 0 and not (
            normalize_billing_priority(entry.get("billing_priority")) == POINTS_FIRST and points_available
        )
        charged_units = 0 if free_used else required_units
        if not free_used and int(entry.get("credit_units") or 0) < charged_units:
            raise QuotaExceeded("temporary token quota exhausted")
        if free_used:
            entry["free_remaining"] = int(entry.get("free_remaining") or 0) - 1
        else:
            entry["credit_units"] = int(entry.get("credit_units") or 0) - charged_units
        if task_id:
            reservations[task_id] = {"status": "reserved", "free": free_used, "units": charged_units, "user_id": str(user_id or ""), "created_at": _now()}
        _prune_reservations(entry)
        _sync_legacy_fields(entry)
        entry["updated_at"] = _now()
        _write_data(data)
        return get_temp_context_from_entry(access.token_hash, entry)


def get_temp_context_from_entry(token_hash: str, entry: dict[str, Any]) -> AccessContext:
    free_remaining = max(0, int(entry.get("free_remaining") or 0))
    credit_units = max(0, int(entry.get("credit_units") or 0))
    return AccessContext(token_hash=token_hash, is_admin=False, is_temp=True, limit=free_remaining + (credit_units + POINT_SCALE - 1) // POINT_SCALE, used=0, remaining=free_remaining + (credit_units + POINT_SCALE - 1) // POINT_SCALE, concurrency=max(1, min(MAX_TEMP_TOKEN_CONCURRENCY, int(entry.get("concurrency") or TEMP_TOKEN_CONCURRENCY))), task_retention_days=normalize_retention_days(entry.get("task_retention_days")), free_remaining=free_remaining, credit_units=credit_units, billing_priority=normalize_billing_priority(entry.get("billing_priority")))


def set_temp_billing_priority(token_hash: str, priority: str) -> dict[str, Any]:
    return update_temp_token(token_hash, billing_priority=normalize_billing_priority(priority))


def refund_temp_quota(access: AccessContext) -> None:
    if not access.is_temp:
        return
    refund_temp_quota_hash(access.token_hash)


def _record_reservation_refund(transaction: dict[str, Any], refund_id: str) -> None:
    user_id = str(transaction.get("user_id") or "")
    units = int(transaction.get("units") or 0)
    video_quota_change = int(transaction.get("video_quota_change") or 0)
    if not user_id or (units <= 0 and video_quota_change == 0):
        return
    from .point_transactions import record_transaction

    record_transaction(
        user_id,
        "video_quota_refund" if video_quota_change else "refund",
        units,
        "视频额度任务退款" if video_quota_change else "任务退款",
        balance_units=int(transaction.get("balance_units") or 0),
        video_quota_change=video_quota_change,
        video_quota_balance=int(transaction.get("video_quota_balance") or 0),
        reference_id=refund_id,
        detail=f"任务 ID：{refund_id}" if refund_id else "",
    )


def refund_temp_quota_hash(token_hash: str, refund_id: str = "") -> bool:
    token_hash = str(token_hash or "").strip().lower()
    if not token_hash:
        return False
    refunded_transaction: dict[str, Any] = {}
    with _LOCK:
        if postgres.enabled():
            def mutate(entry: dict[str, Any]) -> bool:
                if int(entry.get("billing_version") or 0) >= 2 and refund_id:
                    reservation = entry.setdefault("reservations", {}).get(refund_id)
                    if not isinstance(reservation, dict) or reservation.get("status") != "reserved":
                        return False
                    if reservation.get("free"):
                        entry["free_remaining"] = int(entry.get("free_remaining") or 0) + 1
                        refunded_transaction.update({
                            "user_id": str(reservation.get("user_id") or ""),
                            "units": 0,
                            "balance_units": int(entry.get("credit_units") or 0),
                            "video_quota_change": 1,
                            "video_quota_balance": int(entry.get("free_remaining") or 0),
                        })
                    else:
                        units = int(reservation.get("units") or 0)
                        entry["credit_units"] = int(entry.get("credit_units") or 0) + units
                        refunded_transaction.update({
                            "user_id": str(reservation.get("user_id") or ""),
                            "units": units,
                            "balance_units": int(entry.get("credit_units") or 0),
                            "video_quota_balance": int(entry.get("free_remaining") or 0),
                        })
                    reservation["status"] = "refunded"
                    reservation["refunded_at"] = _now()
                    _prune_reservations(entry)
                    _sync_legacy_fields(entry)
                else:
                    refunded = [str(item) for item in entry.get("quota_refund_ids") or [] if item]
                    if refund_id and refund_id in refunded:
                        return False
                    entry["used"] = max(0, int(entry.get("used") or 0) - 1)
                    if refund_id:
                        refunded.append(refund_id)
                        entry["quota_refund_ids"] = refunded[-1000:]
                entry["updated_at"] = _now()
                return True

            try:
                refunded = postgres.mutate_temp_token(token_hash, mutate)
            except KeyError:
                refunded = False
            if refunded:
                _record_reservation_refund(refunded_transaction, refund_id)
            return refunded
        data = _read_data()
        entry = data["tokens"].get(token_hash)
        if not isinstance(entry, dict):
            return False
        if int(entry.get("billing_version") or 0) >= 2 and refund_id:
            reservation = entry.setdefault("reservations", {}).get(refund_id)
            if not isinstance(reservation, dict) or reservation.get("status") != "reserved":
                return False
            if reservation.get("free"):
                entry["free_remaining"] = int(entry.get("free_remaining") or 0) + 1
                refunded_transaction.update({
                    "user_id": str(reservation.get("user_id") or ""),
                    "units": 0,
                    "balance_units": int(entry.get("credit_units") or 0),
                    "video_quota_change": 1,
                    "video_quota_balance": int(entry.get("free_remaining") or 0),
                })
            else:
                units = int(reservation.get("units") or 0)
                entry["credit_units"] = int(entry.get("credit_units") or 0) + units
                refunded_transaction.update({
                    "user_id": str(reservation.get("user_id") or ""),
                    "units": units,
                    "balance_units": int(entry.get("credit_units") or 0),
                    "video_quota_balance": int(entry.get("free_remaining") or 0),
                })
            reservation["status"] = "refunded"
            reservation["refunded_at"] = _now()
            _prune_reservations(entry)
            _sync_legacy_fields(entry)
        else:
            refunded = [str(item) for item in entry.get("quota_refund_ids") or [] if item]
            if refund_id and refund_id in refunded:
                return False
            used = max(0, int(entry.get("used") or 0))
            entry["used"] = max(0, used - 1)
            if refund_id:
                refunded.append(refund_id)
                entry["quota_refund_ids"] = refunded[-1000:]
        entry["updated_at"] = _now()
        _write_data(data)
        _record_reservation_refund(refunded_transaction, refund_id)
        return True
