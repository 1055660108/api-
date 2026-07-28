from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from .config import load_settings, update_config


KEY_PREFIX = "acct_"


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, dict[str, Any]]:
    raw_key = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    hint = f"{raw_key[:9]}...{raw_key[-4:]}"
    update_config(
        {
            "account_access_key_hash": _digest(raw_key),
            "account_access_key_hint": hint,
            "account_access_key_enabled": True,
        }
    )
    return raw_key, status()


def verify_key(value: str) -> bool:
    settings = load_settings()
    supplied = str(value or "").strip()
    expected = str(settings.account_access_key_hash or "")
    return bool(
        settings.account_access_key_enabled
        and supplied.startswith(KEY_PREFIX)
        and expected
        and hmac.compare_digest(_digest(supplied), expected)
    )


def set_enabled(enabled: bool) -> dict[str, Any]:
    settings = load_settings()
    if enabled and not settings.account_access_key_hash:
        raise ValueError("请先生成访问密钥")
    update_config({"account_access_key_enabled": bool(enabled)})
    return status()


def revoke_key() -> dict[str, Any]:
    update_config(
        {
            "account_access_key_hash": "",
            "account_access_key_hint": "",
            "account_access_key_enabled": False,
        }
    )
    return status()


def status() -> dict[str, Any]:
    settings = load_settings()
    return {
        "configured": bool(settings.account_access_key_hash),
        "enabled": bool(settings.account_access_key_enabled and settings.account_access_key_hash),
        "hint": str(settings.account_access_key_hint or ""),
    }
