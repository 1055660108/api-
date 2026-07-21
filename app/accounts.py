from __future__ import annotations

import json
import hashlib
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from typing import Any

from .config import ACCOUNTS_PATH, ensure_dirs
from .platforms import DEFAULT_PLATFORM, normalize_platform
from . import postgres


ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
LOCAL_TZ = timezone(timedelta(hours=8))
_ACCOUNTS_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _quota_charges(account: dict[str, Any]) -> list[dict[str, Any]]:
    charges = account.get("quota_charges")
    if not isinstance(charges, list):
        return []
    return [item for item in charges if isinstance(item, dict) and str(item.get("charge_id") or "")]


def _reconciled_quota_used(account: dict[str, Any]) -> int:
    base = max(0, int(account.get("quota_ledger_base") or 0))
    active = sum(1 for item in _quota_charges(account) if str(item.get("status") or "charged") in {"charged", "settled"})
    return base + active


def _initialize_quota_ledger(account: dict[str, Any]) -> list[dict[str, Any]]:
    if not bool(account.get("quota_ledger_initialized")):
        account["quota_ledger_base"] = max(0, int(account.get("quota_used") or 0))
        account["quota_ledger_initialized"] = True
    charges = _quota_charges(account)
    account["quota_charges"] = charges
    return charges


def _reconcile_account(account: dict[str, Any]) -> bool:
    if not bool(account.get("quota_ledger_initialized")):
        return False
    charges = _quota_charges(account)
    expected = _reconciled_quota_used(account)
    changed = account.get("quota_charges") != charges or max(0, int(account.get("quota_used") or 0)) != expected
    account["quota_charges"] = charges
    account["quota_used"] = expected
    return changed


def _atomic_write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_write_bytes(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_data() -> dict[str, Any]:
    ensure_dirs()
    if postgres.enabled():
        data = postgres.read_document("accounts", {"accounts": []})
        accounts = data.get("accounts")
        return {"accounts": [item for item in accounts if isinstance(item, dict)]} if isinstance(accounts, list) else {"accounts": []}
    if not ACCOUNTS_PATH.exists():
        return {"accounts": []}
    raw = b""
    try:
        raw = ACCOUNTS_PATH.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        try:
            data = json.loads(raw.decode("utf-8-sig", errors="ignore"))
            if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
                raise ValueError("invalid accounts data")
            backup_path = ACCOUNTS_PATH.with_name(f"{ACCOUNTS_PATH.name}.corrupt")
            if not backup_path.exists():
                _atomic_write_bytes(backup_path, raw)
            _write_data(data)
        except Exception as recovery_exc:
            raise RuntimeError(f"accounts data is corrupt: {ACCOUNTS_PATH}") from recovery_exc
    if not isinstance(data, dict):
        raise RuntimeError(f"accounts data is corrupt: {ACCOUNTS_PATH}")
    accounts = data.get("accounts") if isinstance(data, dict) else []
    if not isinstance(accounts, list):
        accounts = []
    return {"accounts": [item for item in accounts if isinstance(item, dict)]}


def _write_data(data: dict[str, Any]) -> None:
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    payload = {"accounts": accounts}
    if postgres.enabled():
        postgres.write_document("accounts", payload)
        return
    _atomic_write(ACCOUNTS_PATH, json.dumps(payload, ensure_ascii=False, indent=2))


def reset_daily_account_quotas_if_needed() -> bool:
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                today = local_today()
                changed = False
                for account in data.get("accounts") or []:
                    if str(account.get("quota_reset_date") or "") == today:
                        if _reconcile_account(account):
                            account["updated_at"] = utc_now()
                            changed = True
                        continue
                    account["quota_used"] = 0
                    account["quota_ledger_base"] = 0
                    account["quota_charges"] = []
                    account["quota_ledger_initialized"] = True
                    account.pop("quota_exhausted_date", None)
                    account["quota_reset_date"] = today
                    account["updated_at"] = utc_now()
                    changed = True
                return changed

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        today = local_today()
        changed = False
        for account in data["accounts"]:
            if str(account.get("quota_reset_date") or "") == today:
                if _reconcile_account(account):
                    account["updated_at"] = utc_now()
                    changed = True
                continue
            account["quota_used"] = 0
            account["quota_ledger_base"] = 0
            account["quota_charges"] = []
            account["quota_ledger_initialized"] = True
            account.pop("quota_exhausted_date", None)
            account["quota_reset_date"] = today
            account["updated_at"] = utc_now()
            changed = True
        if changed:
            _write_data(data)
        return changed


def repair_account_cookie_domains() -> int:
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> int:
                changed = 0
                for account in data.get("accounts") or []:
                    platform = str(account.get("platform") or DEFAULT_PLATFORM)
                    if platform == DEFAULT_PLATFORM:
                        continue
                    target_domain = _default_cookie_domain(platform)
                    account_changed = False
                    for cookie in account.get("cookies") or []:
                        if isinstance(cookie, dict) and str(cookie.get("domain") or "") == ".dola.com":
                            cookie["domain"] = target_domain
                            changed += 1
                            account_changed = True
                    if account_changed:
                        account["cookie_header"] = _cookie_header_from_items(account.get("cookies") or [])
                        account["updated_at"] = utc_now()
                return changed

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        changed = 0
        for account in data["accounts"]:
            platform = str(account.get("platform") or DEFAULT_PLATFORM)
            target_domain = _default_cookie_domain(platform)
            if platform == DEFAULT_PLATFORM:
                continue
            for cookie in account.get("cookies") or []:
                if isinstance(cookie, dict) and str(cookie.get("domain") or "") == ".dola.com":
                    cookie["domain"] = target_domain
                    changed += 1
            if changed:
                account["cookie_header"] = _cookie_header_from_items(account.get("cookies") or [])
                account["updated_at"] = utc_now()
        if changed:
            _write_data(data)
        return changed


def _cookie_header_from_items(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _parse_cookie_header(text: str, platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
    cookie = SimpleCookie()
    cookie.load(text)
    items = []
    for key, morsel in cookie.items():
        if key:
            items.append({"name": key, "value": morsel.value, "domain": _default_cookie_domain(platform), "path": "/"})
    return items


def _default_cookie_domain(platform: str) -> str:
    if platform == "doubao":
        return ".doubao.com"
    if platform == "qianwen":
        return ".tongyi.com"
    return ".dola.com"


def _normalize_cookie_item(item: dict[str, Any], platform: str = DEFAULT_PLATFORM) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    value = str(item.get("value") or "")
    if not name:
        return None
    cookie = {
        "name": name,
        "value": value,
        "domain": str(item.get("domain") or _default_cookie_domain(platform)).strip() or _default_cookie_domain(platform),
        "path": str(item.get("path") or "/").strip() or "/",
    }
    expires = item.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = expires
    same_site = str(item.get("sameSite") or item.get("same_site") or "").strip()
    if same_site in {"Strict", "Lax", "None"}:
        cookie["sameSite"] = same_site
    if isinstance(item.get("httpOnly"), bool):
        cookie["httpOnly"] = item["httpOnly"]
    if isinstance(item.get("secure"), bool):
        cookie["secure"] = item["secure"]
    return cookie


def parse_cookie_payload(raw: str, platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("cookie data is required")
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            source = data["cookies"]
        elif "name" in data and "value" in data:
            source = [data]
        else:
            source = []
        items = [_normalize_cookie_item(item, platform) for item in source if isinstance(item, dict)]
        return [item for item in items if item]
    if isinstance(data, list):
        items = [_normalize_cookie_item(item, platform) for item in data if isinstance(item, dict)]
        return [item for item in items if item]
    items = _parse_cookie_header(text, platform)
    if not items:
        raise ValueError("cookie data is invalid")
    return items


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    cookies = account.get("cookies") if isinstance(account.get("cookies"), list) else []
    cookie_names = [str(item.get("name") or "") for item in cookies if isinstance(item, dict) and item.get("name")]
    quota_limit = max(0, int(account.get("quota_limit") or 0))
    quota_used = max(0, int(account.get("quota_used") or 0))
    return {
        "id": str(account.get("id") or ""),
        "platform": str(account.get("platform") or DEFAULT_PLATFORM),
        "name": str(account.get("name") or ""),
        "enabled": bool(account.get("enabled", True)),
        "account_status": str(account.get("account_status") or ("normal" if account.get("enabled", True) else "abnormal")),
        "status_reason": str(account.get("disabled_reason") or account.get("status_reason") or ""),
        "quota_limit": quota_limit,
        "quota_used": quota_used,
        "quota_remaining": max(0, quota_limit - quota_used) if quota_limit else None,
        "quota_reset_date": str(account.get("quota_reset_date") or ""),
        "current_task_id": str(account.get("current_task_id") or ""),
        "current_worker_id": str(account.get("current_worker_id") or ""),
        "current_started_at": str(account.get("current_started_at") or ""),
        "current_quota_charge_id": str(account.get("current_quota_charge_id") or ""),
        "cookie_count": len(cookie_names),
        "cookie_names": cookie_names[:20],
        "last_used_worker_id": str(account.get("last_used_worker_id") or ""),
        "last_used_at": str(account.get("last_used_at") or ""),
        "created_at": str(account.get("created_at") or ""),
        "updated_at": str(account.get("updated_at") or ""),
    }


def list_accounts(*, include_disabled: bool = True, platform: str | None = None) -> list[dict[str, Any]]:
    with _ACCOUNTS_LOCK:
        accounts = _read_data()["accounts"]
        if platform is not None:
            target_platform = normalize_platform(platform)
            accounts = [item for item in accounts if str(item.get("platform") or DEFAULT_PLATFORM) == target_platform]
        if not include_disabled:
            accounts = [item for item in accounts if item.get("enabled", True)]
        return [_public_account(item) for item in accounts]


def list_account_credentials(platform: str) -> list[dict[str, Any]]:
    target_platform = normalize_platform(platform)
    with _ACCOUNTS_LOCK:
        return [dict(item) for item in _read_data()["accounts"] if str(item.get("platform") or DEFAULT_PLATFORM) == target_platform]


def _account_fingerprint(platform: str, cookies: list[dict[str, Any]]) -> str:
    normalized = sorted(
        (
            str(item.get("name") or ""),
            str(item.get("value") or ""),
            str(item.get("domain") or ""),
            str(item.get("path") or "/"),
        )
        for item in cookies
        if isinstance(item, dict)
    )
    raw = json.dumps([normalize_platform(platform), normalized], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_account(accounts: list[dict[str, Any]], name: str, cookies: list[dict[str, Any]], enabled: bool, quota_limit: int, platform: str) -> dict[str, Any]:
    now = utc_now()
    return {
        "id": secrets.token_hex(8), "platform": platform, "name": str(name or "").strip() or f"账号 {len(accounts) + 1}",
        "enabled": bool(enabled), "quota_limit": max(0, int(quota_limit or 0)), "quota_used": 0,
        "quota_reset_date": local_today(), "cookies": cookies, "cookie_header": _cookie_header_from_items(cookies),
        "cookie_fingerprint": _account_fingerprint(platform, cookies),
        "created_at": now, "updated_at": now, "last_used_worker_id": "", "last_used_at": "",
    }


def add_account(name: str, cookie_data: str, enabled: bool = True, quota_limit: int = 1, platform: str = DEFAULT_PLATFORM) -> dict[str, Any]:
    platform = normalize_platform(platform)
    cookies = parse_cookie_payload(cookie_data, platform)
    if not cookies:
        raise ValueError("cookie data is invalid")
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                accounts = data.setdefault("accounts", [])
                fingerprint = _account_fingerprint(platform, cookies)
                if any(_account_fingerprint(str(item.get("platform") or DEFAULT_PLATFORM), item.get("cookies") or []) == fingerprint for item in accounts):
                    raise ValueError("账号已存在，请勿重复导入")
                account = _new_account(accounts, name, cookies, enabled, quota_limit, platform)
                accounts.append(account)
                return _public_account(account)

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        fingerprint = _account_fingerprint(platform, cookies)
        if any(_account_fingerprint(str(item.get("platform") or DEFAULT_PLATFORM), item.get("cookies") or []) == fingerprint for item in data["accounts"]):
            raise ValueError("账号已存在，请勿重复导入")
        account = _new_account(data["accounts"], name, cookies, enabled, quota_limit, platform)
        data["accounts"].append(account)
        _write_data(data)
        return _public_account(account)


def _split_bulk_line(line: str) -> tuple[str, str, int]:
    text = line.strip()
    for separator in ("----", "\t"):
        if separator in text:
            left, cookie = text.split(separator, 1)
            return left.strip(), cookie.strip(), 0
    parts = [item.strip() for item in text.split("|", 2)]
    if len(parts) == 3 and parts[1].isdigit():
        return parts[0], parts[2], int(parts[1])
    if len(parts) == 2 and "=" in parts[1]:
        return parts[0], parts[1], 0
    return "", text, 0


def parse_bulk_accounts(raw: str, default_quota_limit: int = 1) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("cookie data is required")
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, list):
        rows = []
        for item in data:
            if isinstance(item, dict) and ("cookie_data" in item or "cookies" in item):
                rows.append(
                    {
                        "name": str(item.get("name") or "").strip(),
                        "cookie_data": json.dumps(item.get("cookies"), ensure_ascii=False) if "cookies" in item else str(item.get("cookie_data") or ""),
                        "quota_limit": max(0, int(item.get("quota_limit") or default_quota_limit or 0)),
                    }
                )
        if rows:
            return rows
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, cookie_data, quota_limit = _split_bulk_line(line)
        rows.append({"name": name, "cookie_data": cookie_data, "quota_limit": quota_limit or max(0, int(default_quota_limit or 0))})
    if not rows:
        raise ValueError("cookie data is invalid")
    return rows


def add_accounts_bulk_result(raw: str, default_quota_limit: int = 1, enabled: bool = True, platform: str = DEFAULT_PLATFORM) -> dict[str, Any]:
    rows = parse_bulk_accounts(raw, default_quota_limit)
    platform = normalize_platform(platform)
    prepared = []
    for row in rows:
        cookies = parse_cookie_payload(row.get("cookie_data") or "", platform)
        if not cookies:
            raise ValueError("cookie data is invalid")
        prepared.append((row, cookies, _account_fingerprint(platform, cookies)))
    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            accounts = data.setdefault("accounts", [])
            existing = {
                str(item.get("cookie_fingerprint") or "") or _account_fingerprint(str(item.get("platform") or DEFAULT_PLATFORM), item.get("cookies") or [])
                for item in accounts
            }
            created = []
            skipped = 0
            for row, cookies, fingerprint in prepared:
                if fingerprint in existing:
                    skipped += 1
                    continue
                account = _new_account(accounts, row.get("name") or "", cookies, enabled, int(row.get("quota_limit") or 0), platform)
                accounts.append(account)
                existing.add(fingerprint)
                created.append(_public_account(account))
            return {"accounts": created, "created": len(created), "skipped": skipped, "received": len(prepared)}

        if postgres.enabled():
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        result = mutate(data)
        if result["created"]:
            _write_data(data)
        return result


def add_accounts_bulk(raw: str, default_quota_limit: int = 1, enabled: bool = True, platform: str = DEFAULT_PLATFORM) -> list[dict[str, Any]]:
    return add_accounts_bulk_result(raw, default_quota_limit, enabled, platform)["accounts"]


def merge_duplicate_accounts(account_task_counts: dict[str, int] | None = None) -> dict[str, Any]:
    task_counts = account_task_counts or {}
    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            accounts = data.get("accounts") or []
            groups: dict[str, list[dict[str, Any]]] = {}
            for account in accounts:
                fingerprint = str(account.get("cookie_fingerprint") or "") or _account_fingerprint(str(account.get("platform") or DEFAULT_PLATFORM), account.get("cookies") or [])
                groups.setdefault(fingerprint, []).append(account)
            removed_ids = []
            kept_ids = []
            for fingerprint, duplicates in groups.items():
                if len(duplicates) < 2:
                    duplicates[0]["cookie_fingerprint"] = fingerprint
                    continue
                def score(account: dict[str, Any]) -> tuple[int, int, int, str, str]:
                    charges = _quota_charges(account)
                    return (
                        max(0, int(task_counts.get(str(account.get("id") or ""), 0))),
                        int(bool(str(account.get("current_task_id") or ""))),
                        len(charges),
                        max(0, int(account.get("quota_used") or 0)),
                        str(account.get("updated_at") or account.get("last_used_at") or ""),
                        str(account.get("created_at") or ""),
                    )
                keeper = max(duplicates, key=score)
                keeper["cookie_fingerprint"] = fingerprint
                charge_ids = {str(item.get("charge_id") or "") for item in _quota_charges(keeper)}
                merged_charges = list(_quota_charges(keeper))
                for duplicate in duplicates:
                    if duplicate is keeper:
                        continue
                    for charge in _quota_charges(duplicate):
                        charge_id = str(charge.get("charge_id") or "")
                        if charge_id and charge_id not in charge_ids:
                            merged_charges.append(charge)
                            charge_ids.add(charge_id)
                    removed_ids.append(str(duplicate.get("id") or ""))
                if merged_charges:
                    keeper["quota_charges"] = merged_charges
                    keeper["quota_ledger_initialized"] = True
                    keeper["quota_ledger_base"] = max(max(0, int(item.get("quota_ledger_base") or 0)) for item in duplicates)
                    keeper["quota_used"] = _reconciled_quota_used(keeper)
                keeper["updated_at"] = max(str(item.get("updated_at") or "") for item in duplicates)
                kept_ids.append(str(keeper.get("id") or ""))
            removed_set = set(removed_ids)
            data["accounts"] = [item for item in accounts if str(item.get("id") or "") not in removed_set]
            return {"before": len(accounts), "after": len(data["accounts"]), "removed": len(removed_ids), "removed_ids": removed_ids, "kept_ids": kept_ids}

        if postgres.enabled():
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        result = mutate(data)
        if result["removed"]:
            backup_path = ACCOUNTS_PATH.with_name(f"{ACCOUNTS_PATH.name}.before-dedup-{datetime.now().strftime('%Y%m%d%H%M%S')}.bak")
            _atomic_write(backup_path, json.dumps(_read_data(), ensure_ascii=False, indent=2))
            _write_data(data)
        return result


def delete_account(account_id: str) -> bool:
    account_id = str(account_id or "").strip().lower()
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        return False
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                accounts = data.get("accounts") or []
                next_accounts = [item for item in accounts if str(item.get("id") or "") != account_id]
                data["accounts"] = next_accounts
                return len(next_accounts) != len(accounts)

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        accounts = data["accounts"]
        next_accounts = [item for item in accounts if str(item.get("id") or "") != account_id]
        if len(next_accounts) == len(accounts):
            return False
        data["accounts"] = next_accounts
        _write_data(data)
        return True


def set_account_enabled(account_id: str, enabled: bool) -> dict[str, Any]:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == account_id:
                        account.update(enabled=bool(enabled), updated_at=utc_now())
                        return _public_account(account)
                raise KeyError("account not found")

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == account_id:
                account["enabled"] = bool(enabled)
                account["updated_at"] = utc_now()
                _write_data(data)
                return _public_account(account)
    raise KeyError("account not found")


def update_account_cookies(account_id: str, cookies: list[dict[str, Any]]) -> dict[str, Any]:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") != account_id:
                        continue
                    platform = str(account.get("platform") or DEFAULT_PLATFORM)
                    normalized = [_normalize_cookie_item(item, platform) for item in cookies if isinstance(item, dict)]
                    normalized = [item for item in normalized if item]
                    if not normalized:
                        raise ValueError("cookie data is invalid")
                    now = utc_now()
                    account.update(cookies=normalized, cookie_header=_cookie_header_from_items(normalized), last_cookie_refresh_at=now, enabled=True, updated_at=now)
                    account.pop("disabled_reason", None)
                    return _public_account(account)
                raise KeyError("account not found")

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") != account_id:
                continue
            platform = str(account.get("platform") or DEFAULT_PLATFORM)
            normalized = [_normalize_cookie_item(item, platform) for item in cookies if isinstance(item, dict)]
            normalized = [item for item in normalized if item]
            if not normalized:
                raise ValueError("cookie data is invalid")
            account["cookies"] = normalized
            account["cookie_header"] = _cookie_header_from_items(normalized)
            account["last_cookie_refresh_at"] = utc_now()
            account["enabled"] = True
            account.pop("disabled_reason", None)
            account["updated_at"] = account["last_cookie_refresh_at"]
            _write_data(data)
            return _public_account(account)
    raise KeyError("account not found")


def disable_account_for_login(account_id: str, reason: str) -> dict[str, Any]:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") != account_id:
                        continue
                    normalized_reason = str(reason or "login invalid")[:200]
                    account.update(enabled=False, account_status="abnormal", status_reason=normalized_reason, disabled_reason=normalized_reason, current_task_id="", current_worker_id="", current_started_at="", updated_at=utc_now())
                    return _public_account(account)
                raise KeyError("account not found")

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") != account_id:
                continue
            account["enabled"] = False
            account["account_status"] = "abnormal"
            account["status_reason"] = str(reason or "login invalid")[:200]
            account["disabled_reason"] = str(reason or "login invalid")[:200]
            account["current_task_id"] = ""
            account["current_worker_id"] = ""
            account["current_started_at"] = ""
            account["updated_at"] = utc_now()
            _write_data(data)
            return _public_account(account)
    raise KeyError("account not found")


def set_account_cooldown(account_id: str, seconds: int, reason: str) -> None:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == account_id:
                        account.update(cooldown_until=(datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat(), cooldown_reason=str(reason or "")[:200], updated_at=utc_now())
                        return

            postgres.mutate_document("accounts", {"accounts": []}, mutate)
            return
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") != account_id:
                continue
            account["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat()
            account["cooldown_reason"] = str(reason or "")[:200]
            account["updated_at"] = utc_now()
            _write_data(data)
            return


def account_for_worker(worker_id: str, exclude_ids: set[str] | None = None, platform: str = DEFAULT_PLATFORM) -> dict[str, Any] | None:
    with _ACCOUNTS_LOCK:
        return _select_account(_read_data()["accounts"], exclude_ids, platform)


def _select_account(accounts: list[dict[str, Any]], exclude_ids: set[str] | None = None, platform: str = DEFAULT_PLATFORM) -> dict[str, Any] | None:
    excluded = exclude_ids or set()
    target_platform = normalize_platform(platform)
    now = datetime.now(timezone.utc)

    def available(item: dict[str, Any]) -> bool:
        value = str(item.get("cooldown_until") or "")
        if not value:
            return True
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= now
        except ValueError:
            return True

    enabled_accounts = [
        item for item in accounts
        if item.get("enabled", True)
        and str(item.get("platform") or DEFAULT_PLATFORM) == target_platform
        and str(item.get("id") or "") not in excluded
        and not str(item.get("current_task_id") or "")
        and isinstance(item.get("cookies"), list)
        and item.get("cookies")
        and available(item)
        and str(item.get("quota_exhausted_date") or "") != local_today()
        and (not int(item.get("quota_limit") or 0) or int(item.get("quota_used") or 0) < int(item.get("quota_limit") or 0))
    ]
    if not enabled_accounts:
        return None

    def priority(item: dict[str, Any]) -> tuple[int, int, int, str]:
        quota_limit = max(0, int(item.get("quota_limit") or 0))
        quota_used = max(0, int(item.get("quota_used") or 0))
        quota_remaining = max(0, quota_limit - quota_used) if quota_limit else 1000000
        unused_full_quota = quota_limit == 2 and quota_used == 0 and quota_remaining == 2
        return (0 if unused_full_quota else 1, -quota_remaining, quota_used, str(item.get("last_used_at") or ""))

    enabled_accounts.sort(key=priority)
    account = enabled_accounts[0]
    return {
        "id": str(account.get("id") or ""),
        "platform": str(account.get("platform") or DEFAULT_PLATFORM),
        "name": str(account.get("name") or ""),
        "cookies": [dict(item) for item in account.get("cookies") or [] if isinstance(item, dict)],
        "cookie_header": str(account.get("cookie_header") or ""),
    }


def claim_account_for_worker(worker_id: str, task_id: str, exclude_ids: set[str] | None = None, platform: str = DEFAULT_PLATFORM) -> dict[str, Any] | None:
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
                accounts = data.get("accounts")
                if not isinstance(accounts, list):
                    accounts = []
                    data["accounts"] = accounts
                selected = _select_account(accounts, exclude_ids, platform)
                if not selected:
                    return None
                now = utc_now()
                charge_id = f"{task_id}:{secrets.token_hex(8)}"
                for account in accounts:
                    if str(account.get("id") or "") == selected["id"]:
                        charges = _initialize_quota_ledger(account)
                        charges.append({"charge_id": charge_id, "task_id": str(task_id or ""), "status": "charged", "charged_at": now})
                        account.update(last_used_worker_id=str(worker_id or ""), last_used_at=now, current_task_id=str(task_id or ""), current_worker_id=str(worker_id or ""), current_started_at=now, current_quota_charge_id=charge_id, quota_charges=charges, quota_used=_reconciled_quota_used(account), updated_at=now)
                        selected["quota_charge_id"] = charge_id
                        return selected
                return None

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        selected = _select_account(data["accounts"], exclude_ids, platform)
        if not selected:
            return None
        now = utc_now()
        charge_id = f"{task_id}:{secrets.token_hex(8)}"
        for account in data["accounts"]:
            if str(account.get("id") or "") == selected["id"]:
                charges = _initialize_quota_ledger(account)
                charges.append({"charge_id": charge_id, "task_id": str(task_id or ""), "status": "charged", "charged_at": now})
                account["last_used_worker_id"] = str(worker_id or "")
                account["last_used_at"] = now
                account["current_task_id"] = str(task_id or "")
                account["current_worker_id"] = str(worker_id or "")
                account["current_started_at"] = now
                account["current_quota_charge_id"] = charge_id
                account["quota_charges"] = charges
                account["quota_used"] = _reconciled_quota_used(account)
                account["updated_at"] = now
                _write_data(data)
                selected["quota_charge_id"] = charge_id
                return selected
        return None


def claim_account_for_maintenance(worker_id: str, platform: str) -> dict[str, Any] | None:
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
                accounts = data.get("accounts") or []
                selected = _select_account(accounts, platform=platform)
                if not selected:
                    return None
                now = utc_now()
                for account in accounts:
                    if str(account.get("id") or "") == selected["id"]:
                        account.update(current_task_id=f"maintenance:{worker_id}", current_worker_id=str(worker_id or ""), current_started_at=now, updated_at=now)
                        return selected
                return None

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        selected = _select_account(data["accounts"], platform=platform)
        if not selected:
            return None
        now = utc_now()
        for account in data["accounts"]:
            if str(account.get("id") or "") == selected["id"]:
                account["current_task_id"] = f"maintenance:{worker_id}"
                account["current_worker_id"] = str(worker_id or "")
                account["current_started_at"] = now
                account["updated_at"] = now
                _write_data(data)
                return selected
        return None


def mark_account_used(account_id: str, worker_id: str, task_id: str = "") -> None:
    if not account_id:
        return
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == account_id:
                        now = utc_now()
                        account.update(last_used_worker_id=str(worker_id or ""), last_used_at=now, current_task_id=str(task_id or ""), current_worker_id=str(worker_id or ""), current_started_at=now, quota_used=max(0, int(account.get("quota_used") or 0)) + 1, updated_at=now)
                        return

            postgres.mutate_document("accounts", {"accounts": []}, mutate)
            return
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == account_id:
                account["last_used_worker_id"] = str(worker_id or "")
                account["last_used_at"] = utc_now()
                account["current_task_id"] = str(task_id or "")
                account["current_worker_id"] = str(worker_id or "")
                account["current_started_at"] = account["last_used_at"]
                account["quota_used"] = max(0, int(account.get("quota_used") or 0)) + 1
                account["updated_at"] = account["last_used_at"]
                _write_data(data)
                return


def refund_account_quota(account_id: str, refund_id: str = "") -> bool:
    if not account_id:
        return False
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") != str(account_id):
                        continue
                    if bool(account.get("quota_ledger_initialized")):
                        for charge in _quota_charges(account):
                            if str(charge.get("charge_id") or "") != str(refund_id or "") or str(charge.get("status") or "charged") != "charged":
                                continue
                            charge.update(status="refunded", refunded_at=utc_now())
                            account["quota_used"] = _reconciled_quota_used(account)
                            account["updated_at"] = utc_now()
                            return True
                        return False
                    refunded = [str(item) for item in account.get("quota_refund_ids") or [] if item]
                    if refund_id and refund_id in refunded:
                        return False
                    account["quota_used"] = max(0, int(account.get("quota_used") or 0) - 1)
                    if refund_id:
                        refunded.append(refund_id)
                        account["quota_refund_ids"] = refunded[-1000:]
                    account["updated_at"] = utc_now()
                    return True
                return False

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == str(account_id):
                if bool(account.get("quota_ledger_initialized")):
                    for charge in _quota_charges(account):
                        if str(charge.get("charge_id") or "") != str(refund_id or "") or str(charge.get("status") or "charged") != "charged":
                            continue
                        charge.update(status="refunded", refunded_at=utc_now())
                        account["quota_used"] = _reconciled_quota_used(account)
                        account["updated_at"] = utc_now()
                        _write_data(data)
                        return True
                    return False
                refunded = [str(item) for item in account.get("quota_refund_ids") or [] if item]
                if refund_id and refund_id in refunded:
                    return False
                account["quota_used"] = max(0, int(account.get("quota_used") or 0) - 1)
                if refund_id:
                    refunded.append(refund_id)
                    account["quota_refund_ids"] = refunded[-1000:]
                account["updated_at"] = utc_now()
                _write_data(data)
                return True
    return False


def reconcile_account_quotas() -> dict[str, int]:
    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> dict[str, int]:
            checked = 0
            repaired = 0
            for account in data.get("accounts") or []:
                checked += 1
                if _reconcile_account(account):
                    account["updated_at"] = utc_now()
                    repaired += 1
            return {"checked": checked, "repaired": repaired}

        if postgres.enabled():
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        result = mutate(data)
        if result["repaired"]:
            _write_data(data)
        return result


def reconcile_success_quota_charges(records: list[dict[str, str]], *, dry_run: bool = True) -> dict[str, Any]:
    normalized: list[dict[str, str]] = []
    seen_charge_ids: set[str] = set()
    for record in records:
        task_id = str(record.get("task_id") or "")
        account_id = str(record.get("account_id") or "")
        charge_id = str(record.get("charge_id") or "")
        finished_at = str(record.get("finished_at") or "")
        if not task_id or not account_id or not charge_id or charge_id in seen_charge_ids:
            continue
        seen_charge_ids.add(charge_id)
        normalized.append({"task_id": task_id, "account_id": account_id, "charge_id": charge_id, "finished_at": finished_at})

    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            accounts_by_id = {str(item.get("id") or ""): item for item in data.get("accounts") or []}
            records_by_account: dict[str, list[dict[str, str]]] = {}
            missing_accounts: list[str] = []
            for record in normalized:
                if record["account_id"] not in accounts_by_id:
                    missing_accounts.append(record["account_id"])
                    continue
                records_by_account.setdefault(record["account_id"], []).append(record)

            added = 0
            already_present = 0
            repaired_accounts = 0
            changes: list[dict[str, Any]] = []
            now = utc_now()
            for account_id, account_records in records_by_account.items():
                account = accounts_by_id[account_id]
                before_used = max(0, int(account.get("quota_used") or 0))
                existing_charges = _quota_charges(account)
                existing_ids = {str(item.get("charge_id") or "") for item in existing_charges}
                missing_records = [item for item in account_records if item["charge_id"] not in existing_ids]
                already_present += len(account_records) - len(missing_records)
                if not missing_records:
                    continue
                if not bool(account.get("quota_ledger_initialized")):
                    account["quota_ledger_base"] = max(0, before_used - len(missing_records))
                    account["quota_ledger_initialized"] = True
                for record in missing_records:
                    charge = {
                        "charge_id": record["charge_id"],
                        "task_id": record["task_id"],
                        "status": "settled",
                        "charged_at": record["finished_at"] or now,
                        "settled_at": record["finished_at"] or now,
                        "source": "success_reconciliation",
                    }
                    existing_charges.append(charge)
                account["quota_charges"] = existing_charges
                account["quota_used"] = _reconciled_quota_used(account)
                account["updated_at"] = now
                added += len(missing_records)
                repaired_accounts += 1
                changes.append({
                    "account_id": account_id,
                    "account_name": str(account.get("name") or ""),
                    "before_used": before_used,
                    "after_used": int(account["quota_used"]),
                    "added_charges": len(missing_records),
                })
            return {
                "dry_run": dry_run,
                "input_records": len(records),
                "valid_records": len(normalized),
                "matched_records": len(normalized) - len(missing_accounts),
                "added_charges": added,
                "already_present": already_present,
                "repaired_accounts": repaired_accounts,
                "missing_account_ids": sorted(set(missing_accounts)),
                "quota_used_before": sum(max(0, int(accounts_by_id[account_id].get("quota_used") or 0)) for account_id in records_by_account),
                "changes": changes,
            }

        if postgres.enabled():
            if dry_run:
                return mutate(_read_data())
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        before_by_id = {str(item.get("id") or ""): max(0, int(item.get("quota_used") or 0)) for item in data["accounts"]}
        result = mutate(data)
        matched_account_ids = {item["account_id"] for item in normalized if item["account_id"] in before_by_id}
        result["quota_used_before"] = sum(before_by_id[account_id] for account_id in matched_account_ids)
        result["quota_used_after"] = sum(max(0, int(next(account for account in data["accounts"] if str(account.get("id") or "") == account_id).get("quota_used") or 0)) for account_id in matched_account_ids)
        if not dry_run and result["added_charges"]:
            _write_data(data)
        return result


def settle_account_quota(account_id: str, charge_id: str) -> bool:
    if not account_id or not charge_id:
        return False
    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> bool:
            for account in data.get("accounts") or []:
                if str(account.get("id") or "") != str(account_id):
                    continue
                for charge in _quota_charges(account):
                    if str(charge.get("charge_id") or "") != str(charge_id) or str(charge.get("status") or "charged") != "charged":
                        continue
                    charge.update(status="settled", settled_at=utc_now())
                    account["quota_used"] = _reconciled_quota_used(account)
                    account["updated_at"] = utc_now()
                    return True
                return False
            return False

        if postgres.enabled():
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        settled = mutate(data)
        if settled:
            _write_data(data)
        return settled


def exhaust_timed_out_account(account_id: str, charge_id: str = "") -> bool:
    """Keep a timed-out generation charged and exclude its account for today."""
    if not account_id:
        return False
    with _ACCOUNTS_LOCK:
        def mutate(data: dict[str, Any]) -> bool:
            for account in data.get("accounts") or []:
                if str(account.get("id") or "") != str(account_id):
                    continue
                now = utc_now()
                if charge_id:
                    charges = _initialize_quota_ledger(account)
                    matching_charge = None
                    for charge in charges:
                        if str(charge.get("charge_id") or "") != str(charge_id):
                            continue
                        matching_charge = charge
                        break
                    if matching_charge is None:
                        matching_charge = {"charge_id": str(charge_id), "status": "settled", "settled_at": now, "settle_reason": "result_timeout"}
                        charges.append(matching_charge)
                    else:
                        matching_charge.update(status="settled", settled_at=now, settle_reason="result_timeout")
                        matching_charge.pop("refunded_at", None)
                        matching_charge.pop("refund_reason", None)
                    account["quota_charges"] = charges
                    account["quota_used"] = _reconciled_quota_used(account)
                else:
                    account["quota_used"] = max(1, int(account.get("quota_used") or 0))
                account["quota_exhausted_date"] = local_today()
                account["updated_at"] = now
                return True
            return False

        if postgres.enabled():
            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        exhausted = mutate(data)
        if exhausted:
            _write_data(data)
        return exhausted


def exhaust_account_quota(account_id: str, charge_id: str = "") -> bool:
    if not account_id:
        return False
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> bool:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == str(account_id):
                        quota_limit = max(1, int(account.get("quota_limit") or 1))
                        if charge_id and bool(account.get("quota_ledger_initialized")):
                            for charge in _quota_charges(account):
                                if str(charge.get("charge_id") or "") == str(charge_id) and str(charge.get("status") or "charged") == "charged":
                                    charge.update(status="refunded", refunded_at=utc_now(), refund_reason="quota_insufficient")
                                    break
                            quota_used = _reconciled_quota_used(account)
                        else:
                            quota_used = max(quota_limit, int(account.get("quota_used") or 0))
                        account.update(quota_limit=quota_limit, quota_used=quota_used, quota_exhausted_date=local_today(), updated_at=utc_now())
                        return True
                return False

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == str(account_id):
                quota_limit = max(1, int(account.get("quota_limit") or 1))
                account["quota_limit"] = quota_limit
                if charge_id and bool(account.get("quota_ledger_initialized")):
                    for charge in _quota_charges(account):
                        if str(charge.get("charge_id") or "") == str(charge_id) and str(charge.get("status") or "charged") == "charged":
                            charge.update(status="refunded", refunded_at=utc_now(), refund_reason="quota_insufficient")
                            break
                    account["quota_used"] = _reconciled_quota_used(account)
                else:
                    account["quota_used"] = max(quota_limit, int(account.get("quota_used") or 0))
                account["quota_exhausted_date"] = local_today()
                account["updated_at"] = utc_now()
                _write_data(data)
                return True
    return False


def clear_account_current_task(account_id: str, task_id: str = "") -> None:
    if not account_id:
        return
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") != str(account_id):
                        continue
                    if task_id and str(account.get("current_task_id") or "") != str(task_id):
                        return
                    account.update(current_task_id="", current_worker_id="", current_started_at="", current_quota_charge_id="", updated_at=utc_now())
                    return

            postgres.mutate_document("accounts", {"accounts": []}, mutate)
            return
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == str(account_id):
                if task_id and str(account.get("current_task_id") or "") != str(task_id):
                    return
                account["current_task_id"] = ""
                account["current_worker_id"] = ""
                account["current_started_at"] = ""
                account["current_quota_charge_id"] = ""
                account["updated_at"] = utc_now()
                _write_data(data)
                return


def account_for_current_task(task_id: str) -> dict[str, Any] | None:
    task_id = str(task_id or "")
    if not task_id:
        return None
    with _ACCOUNTS_LOCK:
        for account in _read_data()["accounts"]:
            if str(account.get("current_task_id") or "") == task_id:
                return _public_account(account)
    return None


def reset_account_quota(account_id: str) -> dict[str, Any]:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == account_id:
                        account.update(quota_used=0, quota_ledger_base=0, quota_charges=[], quota_ledger_initialized=True, updated_at=utc_now())
                        account.pop("quota_exhausted_date", None)
                        return _public_account(account)
                raise KeyError("account not found")

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == account_id:
                account["quota_used"] = 0
                account["quota_ledger_base"] = 0
                account["quota_charges"] = []
                account["quota_ledger_initialized"] = True
                account.pop("quota_exhausted_date", None)
                account["updated_at"] = utc_now()
                _write_data(data)
                return _public_account(account)
    raise KeyError("account not found")


def update_account_quota(account_id: str, quota_limit: int) -> dict[str, Any]:
    account_id = str(account_id or "").strip().lower()
    with _ACCOUNTS_LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> dict[str, Any]:
                for account in data.get("accounts") or []:
                    if str(account.get("id") or "") == account_id:
                        account.update(quota_limit=max(0, int(quota_limit or 0)), updated_at=utc_now())
                        return _public_account(account)
                raise KeyError("account not found")

            return postgres.mutate_document("accounts", {"accounts": []}, mutate)
        data = _read_data()
        for account in data["accounts"]:
            if str(account.get("id") or "") == account_id:
                account["quota_limit"] = max(0, int(quota_limit or 0))
                account["updated_at"] = utc_now()
                _write_data(data)
                return _public_account(account)
    raise KeyError("account not found")
