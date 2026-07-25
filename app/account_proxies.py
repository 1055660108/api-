from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from . import config


_STORE_LOCK = threading.RLock()
_ROTATION_LOCK = threading.Lock()
_ROTATION_CURSOR = 0
MAX_ACCOUNT_PROXIES = 300
MAX_IMPORT_BYTES = 256 * 1024


def _store_path() -> Path:
    return config.DATA_DIR / "account_proxies.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_id(scheme: str, host: str, port: int, username: str, password: str) -> str:
    identity = f"{scheme}\0{host}\0{port}\0{username}\0{password}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _country_for(username: str, host: str) -> str:
    searchable = f" {username} {host} ".lower().replace("_", " ").replace("-", " ")
    markers = {
        "香港": (" hong kong ", " hongkong ", " hk "),
        "台湾": (" taiwan ", " taipei ", " tw "),
        "日本": (" japan ", " tokyo ", " osaka ", " jp "),
        "新加坡": (" singapore ", " sg "),
        "美国": (" united states ", " los angeles ", " san jose ", " seattle ", " us "),
        "韩国": (" korea ", " seoul ", " kr "),
        "英国": (" united kingdom ", " london ", " uk "),
        "德国": (" germany ", " frankfurt ", " de "),
        "法国": (" france ", " paris ", " fr "),
        "加拿大": (" canada ", " toronto ", " ca "),
        "澳大利亚": (" australia ", " sydney ", " au "),
    }
    for country, values in markers.items():
        if any(marker in searchable for marker in values):
            return country
    return "未知"


def parse_account_proxy_line(value: str) -> dict[str, Any]:
    line = str(value or "").strip()
    if not line or len(line) > 2000 or any(char in line for char in "\r\n\0"):
        raise ValueError("代理格式无效")
    if "://" not in line and "@" not in line:
        parts = line.split(":", 3)
        if len(parts) != 4:
            raise ValueError("代理格式应为 协议://用户名:密码@主机:端口")
        host, raw_port, username, password = parts
        scheme = "socks5"
        host = config.validate_proxy_account_host(host)
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("代理端口无效") from exc
    else:
        normalized = line if "://" in line else f"socks5://{line}"
        try:
            parsed = urlsplit(normalized)
            scheme = config.validate_proxy_api_scheme(parsed.scheme)
            host = config.validate_proxy_account_host(parsed.hostname)
            port = int(parsed.port or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("代理地址或端口无效") from exc
        username = unquote(str(parsed.username or ""))
        password = unquote(str(parsed.password or ""))
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("代理地址不能包含路径、查询参数或片段")
    username = str(username or "").strip()
    password = str(password or "")
    if port < 1 or port > 65535:
        raise ValueError("代理端口必须在 1-65535 之间")
    if not username or not password:
        raise ValueError("代理用户名和密码不能为空")
    if len(username) > 300 or len(password) > 500:
        raise ValueError("代理用户名或密码过长")
    entry_id = _entry_id(scheme, host, port, username, password)
    return {
        "id": entry_id,
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "country": _country_for(username, host),
        "enabled": True,
        "latency_ms": None,
        "latency_status": "untested",
        "checked_at": "",
        "created_at": _now_iso(),
    }


def _legacy_entry(settings: Any) -> dict[str, Any] | None:
    if not (settings.proxy_account_host and settings.proxy_account_port and settings.proxy_account_username and settings.proxy_account_password):
        return None
    scheme = config.validate_proxy_api_scheme(settings.proxy_account_scheme)
    host = config.validate_proxy_account_host(settings.proxy_account_host)
    port = int(settings.proxy_account_port)
    username = str(settings.proxy_account_username)
    password = str(settings.proxy_account_password)
    return {
        "id": _entry_id(scheme, host, port, username, password),
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "country": _country_for(username, host),
        "enabled": True,
        "latency_ms": None,
        "latency_status": "untested",
        "checked_at": "",
        "created_at": _now_iso(),
    }


def _empty_store() -> dict[str, Any]:
    return {"proxies": [], "selected_ids": [], "rotation_enabled": True, "updated_at": _now_iso()}


def _read_store(settings: Any | None = None) -> dict[str, Any]:
    path = _store_path()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("账密代理列表数据损坏") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("proxies"), list):
            raise RuntimeError("账密代理列表数据损坏")
        payload.setdefault("selected_ids", [])
        payload.setdefault("rotation_enabled", True)
        return payload
    current = settings or config.load_settings()
    store = _empty_store()
    legacy = _legacy_entry(current)
    if legacy:
        store["proxies"] = [legacy]
        store["selected_ids"] = [legacy["id"]]
    return store


def _write_store(store: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = _now_iso()
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _masked_username(username: str) -> str:
    value = str(username or "")
    return f"{value[:3]}***{value[-2:]}" if len(value) > 5 else "***"


def public_account_proxy(entry: dict[str, Any], selected_ids: set[str]) -> dict[str, Any]:
    return {
        "id": str(entry.get("id") or ""),
        "name": f"{entry.get('host')}:{entry.get('port')}",
        "host": str(entry.get("host") or ""),
        "port": int(entry.get("port") or 0),
        "protocol": str(entry.get("scheme") or "socks5"),
        "username_masked": _masked_username(str(entry.get("username") or "")),
        "country": str(entry.get("country") or "未知"),
        "enabled": bool(entry.get("enabled", True)),
        "selected": str(entry.get("id") or "") in selected_ids,
        "latency_ms": int(entry["latency_ms"]) if entry.get("latency_ms") else None,
        "latency_status": str(entry.get("latency_status") or "untested"),
        "checked_at": str(entry.get("checked_at") or ""),
    }


def list_account_proxies(settings: Any | None = None) -> dict[str, Any]:
    with _STORE_LOCK:
        store = _read_store(settings)
        selected_order = list(dict.fromkeys(str(item) for item in store.get("selected_ids", []) if str(item)))
        selected = set(selected_order)
        return {
            "proxies": [public_account_proxy(entry, selected) for entry in store["proxies"]],
            "selected_ids": selected_order,
            "rotation_enabled": bool(store.get("rotation_enabled", True)),
        }


def account_proxy_configured(settings: Any | None = None) -> bool:
    with _STORE_LOCK:
        return bool(_read_store(settings).get("proxies"))


def import_account_proxies(text: str, settings: Any | None = None) -> dict[str, Any]:
    raw = str(text or "")
    if len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValueError("导入内容不能超过 256KB")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise ValueError("请粘贴至少一条代理")
    if len(lines) > MAX_ACCOUNT_PROXIES:
        raise ValueError(f"单次最多导入 {MAX_ACCOUNT_PROXIES} 条代理")
    parsed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, line in enumerate(lines, 1):
        try:
            parsed.append(parse_account_proxy_line(line))
        except ValueError as exc:
            errors.append({"line": index, "error": str(exc)})
    if errors:
        preview = "；".join(f"第{item['line']}行：{item['error']}" for item in errors[:5])
        raise ValueError(preview)
    with _STORE_LOCK:
        store = _read_store(settings)
        existing = {str(item.get("id") or ""): item for item in store["proxies"]}
        added_ids: list[str] = []
        for entry in parsed:
            if entry["id"] not in existing:
                if len(existing) >= MAX_ACCOUNT_PROXIES:
                    raise ValueError(f"账密代理列表最多保存 {MAX_ACCOUNT_PROXIES} 条")
                existing[entry["id"]] = entry
                added_ids.append(entry["id"])
        store["proxies"] = list(existing.values())
        selected = list(dict.fromkeys([str(item) for item in store.get("selected_ids", [])] + added_ids))
        store["selected_ids"] = selected
        _write_store(store)
        result = list_account_proxies(settings)
        return {**result, "added": len(added_ids), "added_ids": added_ids, "duplicates": len(parsed) - len(added_ids)}


def select_account_proxies(proxy_ids: Iterable[str], rotation_enabled: bool, settings: Any | None = None) -> dict[str, Any]:
    requested = list(dict.fromkeys(str(item or "").strip() for item in proxy_ids if str(item or "").strip()))
    with _STORE_LOCK:
        store = _read_store(settings)
        available = {str(item.get("id") or "") for item in store["proxies"] if bool(item.get("enabled", True))}
        if any(item not in available for item in requested):
            raise ValueError("选择中包含不存在或已禁用的账密代理")
        store["selected_ids"] = requested
        store["rotation_enabled"] = bool(rotation_enabled)
        _write_store(store)
        return list_account_proxies(settings)


def set_account_proxies_enabled(proxy_ids: Iterable[str], enabled: bool, settings: Any | None = None) -> dict[str, Any]:
    requested = {str(item or "").strip() for item in proxy_ids if str(item or "").strip()}
    if not requested:
        raise ValueError("请选择账密代理")
    with _STORE_LOCK:
        store = _read_store(settings)
        found = set()
        for entry in store["proxies"]:
            if str(entry.get("id") or "") in requested:
                entry["enabled"] = bool(enabled)
                found.add(str(entry["id"]))
        if found != requested:
            raise ValueError("部分账密代理不存在")
        if not enabled:
            store["selected_ids"] = [item for item in store.get("selected_ids", []) if str(item) not in requested]
        _write_store(store)
        return list_account_proxies(settings)


def delete_account_proxies(proxy_ids: Iterable[str], settings: Any | None = None) -> dict[str, Any]:
    requested = {str(item or "").strip() for item in proxy_ids if str(item or "").strip()}
    if not requested:
        raise ValueError("请选择账密代理")
    with _STORE_LOCK:
        store = _read_store(settings)
        before = len(store["proxies"])
        store["proxies"] = [entry for entry in store["proxies"] if str(entry.get("id") or "") not in requested]
        if before - len(store["proxies"]) != len(requested):
            raise ValueError("部分账密代理不存在")
        store["selected_ids"] = [item for item in store.get("selected_ids", []) if str(item) not in requested]
        _write_store(store)
        return list_account_proxies(settings)


def account_proxy_entries(proxy_ids: Iterable[str] | None = None, settings: Any | None = None) -> list[dict[str, Any]]:
    requested = {str(item or "").strip() for item in (proxy_ids or []) if str(item or "").strip()}
    with _STORE_LOCK:
        store = _read_store(settings)
        return [dict(entry) for entry in store["proxies"] if not requested or str(entry.get("id") or "") in requested]


def update_account_proxy_latencies(results: dict[str, tuple[bool, int | None]], settings: Any | None = None) -> dict[str, Any]:
    with _STORE_LOCK:
        store = _read_store(settings)
        checked_at = _now_iso()
        for entry in store["proxies"]:
            result = results.get(str(entry.get("id") or ""))
            if result is None:
                continue
            available, latency_ms = result
            entry["latency_ms"] = int(latency_ms) if available and latency_ms else None
            entry["latency_status"] = "available" if available else "unavailable"
            entry["checked_at"] = checked_at
        _write_store(store)
        return list_account_proxies(settings)


def account_proxy_candidates(settings: Any | None = None) -> list[dict[str, Any]]:
    global _ROTATION_CURSOR
    with _STORE_LOCK:
        store = _read_store(settings)
        selected = [str(item) for item in store.get("selected_ids", []) if str(item)]
        by_id = {str(entry.get("id") or ""): entry for entry in store["proxies"] if bool(entry.get("enabled", True))}
        candidates = [dict(by_id[item]) for item in selected if item in by_id]
        rotation_enabled = bool(store.get("rotation_enabled", True))
    if not candidates:
        return []
    if not rotation_enabled:
        return candidates[:1]
    with _ROTATION_LOCK:
        start = _ROTATION_CURSOR % len(candidates)
        _ROTATION_CURSOR = (_ROTATION_CURSOR + 1) % max(1, len(candidates))
    return candidates[start:] + candidates[:start]


def account_proxy_url(entry: dict[str, Any]) -> str:
    return config.account_proxy_url_for(
        str(entry.get("scheme") or "socks5"),
        str(entry.get("host") or ""),
        int(entry.get("port") or 0),
        str(entry.get("username") or ""),
        str(entry.get("password") or ""),
    )


def account_browser_config(entry: dict[str, Any]) -> dict[str, str] | None:
    return config.account_browser_proxy_config_for(
        str(entry.get("scheme") or "socks5"),
        str(entry.get("host") or ""),
        int(entry.get("port") or 0),
        str(entry.get("username") or ""),
        str(entry.get("password") or ""),
    )
