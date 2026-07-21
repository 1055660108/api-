from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from .billing import points_to_units, units_to_points
from .config import DATA_DIR, ensure_dirs
from .temp_access import add_temp_credit_units, create_temp_tokens, deduct_temp_points, delete_temp_token, hash_token, list_temp_tokens, migrate_temp_token, purchase_temp_membership, rotate_temp_token, update_temp_token
from . import postgres


USERS_PATH = DATA_DIR / "users.json"
_LOCK = threading.RLock()
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,24}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _membership_remaining_seconds(item: dict[str, Any], now: datetime) -> float:
    remaining = max(0.0, float(item.get("remaining_seconds") or 0))
    if str(item.get("status") or "") != "active":
        return remaining
    activated_at = _parse_datetime(item.get("activated_at"))
    return max(0.0, remaining - max(0.0, (now - activated_at).total_seconds())) if activated_at else remaining


def _membership_rank(item: dict[str, Any]) -> tuple[int, str]:
    try:
        price_units = points_to_units(item.get("points_cost") or 0.1)
    except ValueError:
        price_units = 0
    return price_units, str(item.get("purchased_at") or "")


def _normalize_membership_record(item: dict[str, Any]) -> dict[str, Any]:
    record = {
        "id": str(item.get("id") or secrets.token_hex(8)),
        "package_id": str(item.get("package_id") or ""),
        "name": str(item.get("name") or "会员"),
        "points_cost": units_to_points(max(0, int(item.get("points_cost_units") or 0))) if item.get("points_cost_units") is not None else item.get("points_cost", 0),
        "duration_days": max(1, int(item.get("duration_days") or 1)),
        "remaining_seconds": max(0.0, float(item.get("remaining_seconds") or 0)),
        "status": str(item.get("status") or "paused"),
        "activated_at": str(item.get("activated_at") or ""),
        "purchased_at": str(item.get("purchased_at") or _now()),
        "concurrency": max(0, int(item.get("concurrency") or item.get("concurrency_bonus") or 0)),
        "bonus_free_uses": max(0, int(item.get("bonus_free_uses") or 0)),
        "task_discount_points": item.get("task_discount_points", 0),
    }
    if record["status"] not in {"active", "paused", "expired"}:
        record["status"] = "paused"
    try:
        record["points_cost"] = units_to_points(points_to_units(record["points_cost"] or 0.1))
    except ValueError:
        record["points_cost"] = 0
    try:
        record["task_discount_points"] = units_to_points(points_to_units(record["task_discount_points"] or 0.1))
    except ValueError:
        record["task_discount_points"] = 0
    return record


def _sync_membership_state(entry: dict[str, Any], now: datetime | None = None) -> tuple[dict[str, Any] | None, bool]:
    current_time = now or datetime.now(timezone.utc)
    before = json.dumps({"membership": entry.get("membership"), "memberships": entry.get("memberships")}, ensure_ascii=False, sort_keys=True)
    raw_records = entry.get("memberships")
    records = [_normalize_membership_record(item) for item in raw_records if isinstance(item, dict)] if isinstance(raw_records, list) else []
    legacy = entry.get("membership")
    if not records and isinstance(legacy, dict):
        expires_at = _parse_datetime(legacy.get("expires_at"))
        remaining = max(0.0, (expires_at - current_time).total_seconds()) if expires_at else 0.0
        records.append(_normalize_membership_record({
            **legacy,
            "remaining_seconds": remaining,
            "duration_days": max(1, int(legacy.get("duration_days") or max(1, round(remaining / 86400)))),
            "status": "active" if remaining > 0 else "expired",
            "activated_at": current_time.isoformat() if remaining > 0 else "",
        }))

    while True:
        active = next((item for item in records if item["status"] == "active"), None)
        if active and _membership_remaining_seconds(active, current_time) <= 0:
            active["remaining_seconds"] = 0
            active["status"] = "expired"
            active["activated_at"] = ""
            continue
        eligible = [item for item in records if item["status"] != "expired" and _membership_remaining_seconds(item, current_time) > 0]
        if not eligible:
            active = None
            break
        highest = max(eligible, key=_membership_rank)
        if active is highest:
            break
        if active:
            active["remaining_seconds"] = _membership_remaining_seconds(active, current_time)
            active["status"] = "paused"
            active["activated_at"] = ""
        highest["status"] = "active"
        highest["activated_at"] = current_time.isoformat()
        active = highest
        break

    active = next((item for item in records if item["status"] == "active"), None)
    for item in records:
        if item is not active and item["status"] != "expired":
            item["status"] = "paused"
            item["activated_at"] = ""
    entry["memberships"] = records
    if active:
        effective_concurrency = _membership_concurrency(active)
        remaining = _membership_remaining_seconds(active, current_time)
        activated_at = _parse_datetime(active.get("activated_at")) or current_time
        entry["membership"] = {
            **active,
            "expires_at": (activated_at + timedelta(seconds=float(active.get("remaining_seconds") or 0))).isoformat(),
            "effective_concurrency": effective_concurrency,
            "purchased_package_ids": [item["package_id"] for item in records if item["status"] != "expired" and item["package_id"]],
        }
    else:
        entry["membership"] = None
    after = json.dumps({"membership": entry.get("membership"), "memberships": entry.get("memberships")}, ensure_ascii=False, sort_keys=True)
    return dict(entry["membership"]) if isinstance(entry.get("membership"), dict) else None, before != after


def _public_memberships(entry: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    current_time = now or datetime.now(timezone.utc)
    rows = []
    for item in entry.get("memberships") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["remaining_seconds"] = _membership_remaining_seconds(item, current_time)
        row["remaining_days"] = round(row["remaining_seconds"] / 86400, 4)
        rows.append(row)
    return sorted(rows, key=lambda item: (item.get("status") != "active", -_membership_rank(item)[0], item.get("purchased_at", "")))


def _active_membership(entry: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    membership = entry.get("membership")
    if not isinstance(membership, dict):
        return None
    try:
        expires_at = datetime.fromisoformat(str(membership.get("expires_at") or ""))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return membership if expires_at > (now or datetime.now(timezone.utc)) else None


def _membership_concurrency(membership: dict[str, Any] | None) -> int:
    if not isinstance(membership, dict):
        return 1
    return max(1, min(100, int(membership.get("concurrency") or membership.get("concurrency_bonus") or 1)))


def _read() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        data = postgres.read_document("users", {"users": {}})
        if not isinstance(data.get("users"), dict):
            raise RuntimeError("user data is corrupt: PostgreSQL users document")
        return data
    if not USERS_PATH.exists():
        return {"users": {}}
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"user data is corrupt: {USERS_PATH}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise RuntimeError(f"user data is corrupt: {USERS_PATH}")
    return data


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("users", data)
        return
    tmp = USERS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USERS_PATH)


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000).hex()


def register_user(username: str, password: str, email: str = "") -> dict[str, Any]:
    username = str(username or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError("用户名需为3-24位中文、字母、数字或下划线")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度需为8-128位")
    key = username.casefold()
    normalized_email = str(email or "").strip().lower()
    with _LOCK:
        data = _read()
        if key in data["users"]:
            raise ValueError("用户名已存在")
        if normalized_email and any(str(item.get("email") or "").casefold() == normalized_email.casefold() for item in data["users"].values()):
            raise ValueError("邮箱已注册")
        token_entry = create_temp_tokens(1, 1, concurrency=1, task_retention_days=7, remark=username)[0]
        salt = secrets.token_bytes(16)
        now = _now()
        entry = {
            "id": secrets.token_hex(12),
            "username": username,
            "email": normalized_email,
            "email_verified_at": now if normalized_email else "",
            "password_salt": salt.hex(),
            "password_hash": _password_hash(password, salt),
            "token_hash": token_entry["id"],
            "token": token_entry["token"],
            "points_purchased": 0,
            "free_limit": 1,
            "base_concurrency": 1,
            "effective_concurrency": 1,
            "created_at": now,
            "last_login_at": now,
            "last_seen_at": now,
            "enabled": True,
        }
        data["users"][key] = entry
        _write(data)
        return {"username": username, "token": entry["token"]}


def login_user(identifier: str, password: str) -> dict[str, Any] | None:
    value = str(identifier or "").strip().casefold()
    with _LOCK:
        data = _read()
        candidate = next(
            (
                (key, item)
                for key, item in data["users"].items()
                if isinstance(item, dict)
                and value
                and value in {
                    str(item.get("username") or "").casefold(),
                    str(item.get("email") or "").casefold(),
                }
            ),
            None,
        )
        if candidate is None or not candidate[1].get("enabled", True):
            return None
        key, entry = candidate
        salt_hex = str(entry.get("password_salt") or "")
        expected_hash = str(entry.get("password_hash") or "")
    try:
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(_password_hash(password, salt), expected_hash):
        return None
    with _LOCK:
        data = _read()
        entry = data["users"].get(key)
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            return None
        if str(entry.get("password_salt") or "") != salt_hex or not hmac.compare_digest(str(entry.get("password_hash") or ""), expected_hash):
            return None
        now = _now()
        entry["last_login_at"] = now
        entry["last_seen_at"] = now
        _write(data)
        return {"username": entry["username"], "token": entry["token"]}


def has_verified_enabled_email(email: str) -> bool:
    normalized_email = str(email or "").strip().lower()
    with _LOCK:
        return any(
            isinstance(item, dict)
            and str(item.get("email") or "").strip().lower() == normalized_email
            and item.get("email_verified_at")
            and item.get("enabled", True)
            for item in _read()["users"].values()
        )


def reset_user_password_by_email(email: str, new_password: str) -> dict[str, Any]:
    normalized_email = str(email or "").strip().lower()
    if len(new_password) < 8 or len(new_password) > 128:
        raise ValueError("密码长度需为8-128位")
    with _LOCK:
        data = _read()
        entry = next(
            (
                item
                for item in data["users"].values()
                if isinstance(item, dict)
                and str(item.get("email") or "").strip().lower() == normalized_email
                and item.get("email_verified_at")
            ),
            None,
        )
        if not entry or not entry.get("enabled", True):
            raise KeyError(normalized_email)
        replacement_salt = secrets.token_bytes(16)
        old_token_hash = str(entry.get("token_hash") or "")
        token = rotate_temp_token(old_token_hash)
        now = _now()
        entry["password_salt"] = replacement_salt.hex()
        entry["password_hash"] = _password_hash(new_password, replacement_salt)
        entry["token_hash"] = token["id"]
        entry["token"] = token["token"]
        entry["last_seen_at"] = now
        entry["updated_at"] = now
        _write(data)
        return {"username": entry["username"], "token": entry["token"], "_old_token_hash": old_token_hash}


def change_user_password_by_token_hash(token_hash: str, current_password: str, new_password: str) -> dict[str, Any]:
    if len(new_password) < 8 or len(new_password) > 128:
        raise ValueError("密码长度需为8-128位")
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not entry or not entry.get("enabled", True):
            raise KeyError(token_hash)
        salt = bytes.fromhex(str(entry.get("password_salt") or ""))
        if not hmac.compare_digest(_password_hash(current_password, salt), str(entry.get("password_hash") or "")):
            raise ValueError("当前密码错误")
        if hmac.compare_digest(current_password, new_password):
            raise ValueError("新密码不能与当前密码相同")
        replacement_salt = secrets.token_bytes(16)
        token = rotate_temp_token(token_hash)
        entry["password_salt"] = replacement_salt.hex()
        entry["password_hash"] = _password_hash(new_password, replacement_salt)
        entry["token_hash"] = token["id"]
        entry["token"] = token["token"]
        entry["last_seen_at"] = _now()
        entry["updated_at"] = _now()
        _write(data)
        return {"username": entry["username"], "token": entry["token"]}


def user_profile_by_token_hash(token_hash: str) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not entry or not entry.get("enabled", True):
            raise KeyError(token_hash)
        membership, changed = _sync_membership_state(entry)
        if changed:
            _write(data)
        return {
            "username": str(entry.get("username") or ""),
            "email": str(entry.get("email") or ""),
            "email_verified_at": str(entry.get("email_verified_at") or ""),
            "membership": dict(membership) if membership else None,
            "memberships": _public_memberships(entry),
        }


def user_identity_by_token_hash(token_hash: str) -> dict[str, Any]:
    with _LOCK:
        entry = next((item for item in _read()["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not entry or not entry.get("enabled", True):
            raise KeyError(token_hash)
        return {"id": str(entry.get("id") or ""), "username": str(entry.get("username") or ""), "email": str(entry.get("email") or "")}


def change_user_email_by_token_hash(token_hash: str, email: str) -> dict[str, Any]:
    normalized_email = str(email or "").strip().lower()
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not entry or not entry.get("enabled", True):
            raise KeyError(token_hash)
        if any(item is not entry and str(item.get("email") or "").casefold() == normalized_email.casefold() for item in data["users"].values()):
            raise ValueError("邮箱已被其他账号绑定")
        now = _now()
        entry["email"] = normalized_email
        entry["email_verified_at"] = now
        entry["updated_at"] = now
        _write(data)
        return {"email": normalized_email, "email_verified_at": now}


def touch_user_by_token(token: str) -> None:
    token_hash = hash_token(token)
    with _LOCK:
        data = _read()
        for entry in data["users"].values():
            if str(entry.get("token_hash") or "") == token_hash:
                entry["last_seen_at"] = _now()
                _write(data)
                return


def user_token_is_enabled(token_hash: str) -> bool:
    with _LOCK:
        for entry in _read()["users"].values():
            if str(entry.get("token_hash") or "") == str(token_hash or ""):
                return bool(entry.get("enabled", True))
    return True


def list_users(temp_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    quota = {str(item.get("id") or ""): item for item in temp_entries}
    now = datetime.now(timezone.utc)
    with _LOCK:
        data = _read()
        membership_changed = False
        rows = []
        for entry in data["users"].values():
            q = quota.get(str(entry.get("token_hash") or ""), {})
            free_limit = max(1, int(entry.get("free_limit") or 3))
            migrated = int(q.get("credit_units") or 0) > 0 or "free_remaining" in q
            used = max(0, int(q.get("used") or 0))
            free_remaining = max(0, int(q.get("free_remaining") or 0)) if migrated else max(0, free_limit - used)
            points = units_to_points(int(q.get("credit_units") or 0)) if migrated else max(0, max(free_limit, int(q.get("limit") or free_limit)) - max(used, free_limit))
            seen = datetime.fromisoformat(str(entry.get("last_seen_at") or entry.get("created_at")))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            membership, changed = _sync_membership_state(entry, now)
            membership_changed = membership_changed or changed
            rows.append({
                "id": entry["id"], "username": entry["username"], "created_at": entry["created_at"],
                "email": str(entry.get("email") or ""), "email_verified_at": str(entry.get("email_verified_at") or ""),
                "last_login_at": entry.get("last_login_at", ""), "last_seen_at": entry.get("last_seen_at", ""),
                "online": now - seen.astimezone(timezone.utc) <= timedelta(seconds=75),
                "free_remaining": free_remaining, "points": points,
                "used": used, "enabled": bool(entry.get("enabled", True)), "token": str(q.get("token") or entry.get("token") or ""),
                "concurrency": max(1, int(q.get("concurrency") or 1)),
                "base_concurrency": max(1, int(entry.get("base_concurrency") or q.get("concurrency") or 1)),
                "membership": dict(membership) if membership else None,
            })
        if membership_changed:
            _write(data)
        return sorted(rows, key=lambda item: item["created_at"], reverse=True)


def user_balance_by_token_hash(token_hash: str, temp_entries: list[dict[str, Any]]) -> dict[str, int | float]:
    quota = {str(item.get("id") or ""): item for item in temp_entries}
    q = quota.get(str(token_hash or ""), {})
    with _LOCK:
        entry = next((item for item in _read()["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), {})
    free_limit = max(1, int(entry.get("free_limit") or 3))
    migrated = int(q.get("credit_units") or 0) > 0 or "free_remaining" in q
    used = max(0, int(q.get("used") or 0))
    return {
        "free_remaining": max(0, int(q.get("free_remaining") or 0)) if migrated else max(0, free_limit - used),
        "points": units_to_points(int(q.get("credit_units") or 0)) if migrated else max(0, max(free_limit, int(q.get("limit") or free_limit)) - max(used, free_limit)),
    }


def repair_registered_user_tokens() -> int:
    tokens = {str(item.get("id") or ""): item for item in list_temp_tokens()}
    repaired = 0
    with _LOCK:
        data = _read()
        for entry in data["users"].values():
            free_limit = max(1, int(entry.get("free_limit") or 3))
            token_hash = str(entry.get("token_hash") or "")
            token = tokens.get(token_hash)
            if token:
                if migrate_temp_token(token_hash, free_limit):
                    repaired += 1
                continue
            replacement = create_temp_tokens(1, free_limit, concurrency=1, task_retention_days=7, remark=str(entry.get("username") or ""))[0]
            entry["token_hash"] = replacement["id"]
            entry["token"] = replacement["token"]
            repaired += 1
        if repaired:
            _write(data)
    return repaired


def add_user_points(user_id: str, amount: object, temp_entries: list[dict[str, Any]]) -> dict[str, int | float]:
    purchased_units = points_to_units(amount)
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if item.get("id") == user_id), None)
        if not entry:
            raise KeyError(user_id)
        add_temp_credit_units(entry["token_hash"], purchased_units)
        entry["points_purchased_units"] = int(entry.get("points_purchased_units") or int(entry.get("points_purchased") or 0) * 10) + purchased_units
        entry["points_purchased"] = units_to_points(entry["points_purchased_units"])
        _write(data)
        return {"purchased": units_to_points(purchased_units), "credited": units_to_points(purchased_units)}


def deduct_user_points(user_id: str, amount: object) -> None:
    points_to_units(amount)
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if item.get("id") == user_id), None)
        if not entry:
            raise KeyError(user_id)
        deduct_temp_points(str(entry.get("token_hash") or ""), max(1, int(entry.get("free_limit") or 1)), amount)


def purchase_user_membership(user_id: str, package: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if item.get("id") == user_id), None)
        if not isinstance(entry, dict):
            raise KeyError(user_id)
        token_hash = str(entry.get("token_hash") or "")
        token = next((item for item in list_temp_tokens() if str(item.get("id") or "") == token_hash), {})
        base_concurrency = max(1, int(entry.get("base_concurrency") or token.get("concurrency") or 1))
        current_membership, _ = _sync_membership_state(entry, now)
        package_id = str(package.get("id") or "")
        active_records = [item for item in entry.get("memberships") or [] if isinstance(item, dict) and item.get("status") != "expired" and _membership_remaining_seconds(item, now) > 0]
        if any(str(item.get("package_id") or "") == package_id for item in active_records):
            raise ValueError("当前会员有效期内，该会员套餐只能购买一次")
        from .membership_catalog import list_memberships

        catalog = list_memberships()
        highest_price = max((points_to_units(item.get("points_cost")) for item in catalog), default=0)
        if active_records and max((_membership_rank(item)[0] for item in active_records), default=0) >= highest_price > 0:
            raise ValueError("最高级会员有效期内不能购买其他会员套餐")
        membership_concurrency = max(1, min(100, int(package.get("concurrency") or 1)))
        effective_concurrency = membership_concurrency
        balance = purchase_temp_membership(
            token_hash,
            max(1, int(entry.get("free_limit") or 1)),
            package.get("points_cost"),
            int(package.get("bonus_free_uses") or 0),
            effective_concurrency,
        )
        entry.setdefault("memberships", []).append({
            "id": secrets.token_hex(8),
            "package_id": package_id,
            "name": str(package.get("name") or "会员"),
            "points_cost": package.get("points_cost", 0),
            "duration_days": int(package.get("duration_days") or 1),
            "remaining_seconds": int(package.get("duration_days") or 1) * 86400,
            "status": "paused",
            "activated_at": "",
            "concurrency": membership_concurrency,
            "bonus_free_uses": int(package.get("bonus_free_uses") or 0),
            "task_discount_points": package.get("task_discount_points", 0.1),
            "purchased_at": now.isoformat(),
        })
        entry["base_concurrency"] = base_concurrency
        membership, _ = _sync_membership_state(entry, now)
        effective_concurrency = int((membership or {}).get("effective_concurrency") or base_concurrency)
        update_temp_token(token_hash, concurrency=effective_concurrency)
        entry["effective_concurrency"] = effective_concurrency
        entry["updated_at"] = _now()
        _write(data)
        balance["concurrency"] = effective_concurrency
        return {"membership": dict(membership) if membership else None, "memberships": _public_memberships(entry, now), "balance": balance}


def sync_user_membership_by_token_hash(token_hash: str) -> bool:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not isinstance(entry, dict):
            return False
        active, membership_changed = _sync_membership_state(entry)
        token = next((item for item in list_temp_tokens() if str(item.get("id") or "") == str(token_hash or "")), {})
        base_concurrency = max(1, int(entry.get("base_concurrency") or token.get("concurrency") or 1))
        effective_concurrency = _membership_concurrency(active) if active else base_concurrency
        token_concurrency = max(1, int(token.get("concurrency") or 1))
        if not membership_changed and int(entry.get("effective_concurrency") or 0) == effective_concurrency and token_concurrency == effective_concurrency and entry.get("base_concurrency"):
            return False
        update_temp_token(str(token_hash or ""), concurrency=effective_concurrency)
        entry["effective_concurrency"] = effective_concurrency
        entry["base_concurrency"] = base_concurrency
        entry["updated_at"] = _now()
        _write(data)
        return True


def membership_task_discount_units_by_token_hash(token_hash: str) -> int:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not isinstance(entry, dict):
            return 0
        active, changed = _sync_membership_state(entry)
        if changed:
            _write(data)
        if not active:
            return 0
        try:
            return points_to_units(active.get("task_discount_points") or 0.1)
        except ValueError:
            return 0


def rotate_user_token_by_hash(token_hash: str) -> dict[str, Any]:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not entry or not entry.get("enabled", True):
            raise KeyError(token_hash)
        token = rotate_temp_token(token_hash)
        entry["token_hash"] = token["id"]
        entry["token"] = token["token"]
        entry["last_seen_at"] = _now()
        _write(data)
        return {"username": entry["username"], "token": entry["token"]}


def set_user_enabled(user_id: str, enabled: bool) -> None:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if item.get("id") == user_id), None)
        if not entry:
            raise KeyError(user_id)
        entry["enabled"] = bool(enabled)
        entry["updated_at"] = _now()
        _write(data)


def set_user_concurrency(user_id: str, concurrency: int) -> None:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if item.get("id") == user_id), None)
        if not entry:
            raise KeyError(user_id)
        effective_concurrency = _set_effective_concurrency(entry, concurrency)
        _write(data)


def _set_effective_concurrency(entry: dict[str, Any], concurrency: int) -> int:
    requested = max(1, min(100, int(concurrency)))
    active, _ = _sync_membership_state(entry)
    if active:
        active_id = str(active.get("id") or "")
        for membership in entry.get("memberships") or []:
            if isinstance(membership, dict) and str(membership.get("id") or "") == active_id:
                membership["concurrency"] = requested
                break
        active, _ = _sync_membership_state(entry)
        effective_concurrency = _membership_concurrency(active)
    else:
        entry["base_concurrency"] = requested
        effective_concurrency = requested
    update_temp_token(str(entry.get("token_hash") or ""), concurrency=effective_concurrency)
    entry["effective_concurrency"] = effective_concurrency
    entry["updated_at"] = _now()
    return effective_concurrency


def set_user_concurrency_by_token_hash(token_hash: str, concurrency: int) -> int | None:
    with _LOCK:
        data = _read()
        entry = next((item for item in data["users"].values() if str(item.get("token_hash") or "") == str(token_hash or "")), None)
        if not isinstance(entry, dict):
            return None
        effective_concurrency = _set_effective_concurrency(entry, concurrency)
        _write(data)
        return effective_concurrency


def delete_user(user_id: str) -> None:
    with _LOCK:
        data = _read()
        key = next((name for name, item in data["users"].items() if item.get("id") == user_id), None)
        if key is None:
            raise KeyError(user_id)
        entry = data["users"].pop(key)
        delete_temp_token(str(entry.get("token_hash") or ""))
        _write(data)
