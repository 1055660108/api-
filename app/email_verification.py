from __future__ import annotations

import hashlib
import hmac
import ast
import json
import secrets
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import postgres
from .config import DATA_DIR, Settings


EMAIL_VERIFICATIONS_PATH = DATA_DIR / "email_verifications.json"
_LOCK = threading.RLock()


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or email.count("@") != 1:
        raise ValueError("请输入有效的邮箱地址")
    local, domain = email.rsplit("@", 1)
    if not local or len(local) > 64 or not domain or any(char.isspace() for char in email):
        raise ValueError("请输入有效的邮箱地址")
    return email


def normalize_domains(values: Any) -> list[str]:
    if isinstance(values, list):
        source = values
    else:
        raw = str(values or "").strip().replace("，", ",")
        raw_without_prefix = raw.lstrip("@").strip()
        source = None
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(raw_without_prefix)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, (list, tuple)):
                source = list(parsed)
                break
        if source is None:
            source = raw_without_prefix.split(",")
    result: list[str] = []
    for item in source:
        domain = str(item or "").strip().lower().lstrip("@")
        if not domain:
            continue
        if len(domain) > 253 or "." not in domain or any(not (char.isalnum() or char in ".-") for char in domain):
            raise ValueError(f"无效邮箱后缀：@{domain}")
        if domain not in result:
            result.append(domain)
    if not result:
        raise ValueError("至少配置一个允许注册的邮箱后缀")
    return result[:50]


def validate_allowed_email(value: str, settings: Settings) -> str:
    email = normalize_email(value)
    domain = email.rsplit("@", 1)[1]
    if domain not in settings.registration_email_domains:
        raise ValueError("该邮箱后缀暂不支持注册")
    return email


def _read() -> dict[str, Any]:
    if postgres.enabled():
        data = postgres.read_document("email_verifications", {"codes": {}})
        return data if isinstance(data.get("codes"), dict) else {"codes": {}}
    if not EMAIL_VERIFICATIONS_PATH.exists():
        return {"codes": {}}
    try:
        data = json.loads(EMAIL_VERIFICATIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("email verification data is corrupt") from exc
    return data if isinstance(data, dict) and isinstance(data.get("codes"), dict) else {"codes": {}}


def _write(data: dict[str, Any]) -> None:
    if postgres.enabled():
        postgres.write_document("email_verifications", data)
        return
    EMAIL_VERIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = EMAIL_VERIFICATIONS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(EMAIL_VERIFICATIONS_PATH)


def _digest(email: str, code: str, secret: str, purpose: str = "register", scope: str = "") -> str:
    return hmac.new(secret.encode("utf-8"), f"{purpose}:{scope}:{email}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def _send_qq_email(email: str, code: str, settings: Settings) -> None:
    username = settings.registration_smtp_username
    authorization_code = settings.registration_smtp_authorization_code
    if not username or not authorization_code:
        raise RuntimeError("QQ 邮箱 SMTP 尚未配置")
    message = EmailMessage()
    message["Subject"] = "注册邮箱验证码"
    message["From"] = f"{settings.registration_email_sender_name} <{username}>"
    message["To"] = email
    message.set_content(f"您的注册验证码是：{code}\n\n验证码 {settings.registration_email_code_ttl_minutes} 分钟内有效，请勿转发给他人。")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.registration_smtp_host, settings.registration_smtp_port, timeout=15, context=context) as client:
        client.login(username, authorization_code)
        client.send_message(message)


def send_registration_code(email_value: str, settings: Settings, purpose: str = "register", scope: str = "", allow_unlisted: bool = False) -> None:
    if not settings.registration_email_verification_enabled:
        raise ValueError("邮箱注册验证未启用")
    email = normalize_email(email_value) if allow_unlisted else validate_allowed_email(email_value, settings)
    storage_key = f"{purpose}:{scope}:{email}"
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    entry = {
        "digest": _digest(email, code, settings.api_token, purpose, scope),
        "expires_at": now + settings.registration_email_code_ttl_minutes * 60,
        "attempts": 0,
        "sent_at": now,
    }
    with _LOCK:
        if postgres.enabled():
            def mutate(data: dict[str, Any]) -> None:
                codes = data.setdefault("codes", {})
                codes[storage_key] = entry

            postgres.mutate_document("email_verifications", {"codes": {}}, mutate)
        else:
            data = _read()
            data["codes"][storage_key] = entry
            _write(data)
    try:
        _send_qq_email(email, code, settings)
    except Exception:
        with _LOCK:
            if postgres.enabled():
                postgres.mutate_document("email_verifications", {"codes": {}}, lambda data: data.setdefault("codes", {}).pop(storage_key, None))
            else:
                data = _read()
                data["codes"].pop(storage_key, None)
                _write(data)
        raise


def consume_registration_code(email_value: str, code_value: str, settings: Settings, purpose: str = "register", scope: str = "", allow_unlisted: bool = False) -> str:
    email = normalize_email(email_value) if allow_unlisted else validate_allowed_email(email_value, settings)
    storage_key = f"{purpose}:{scope}:{email}"
    code = str(code_value or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("邮箱验证码错误")

    def consume(data: dict[str, Any]) -> str:
        codes = data.setdefault("codes", {})
        entry = codes.get(storage_key)
        if not isinstance(entry, dict) or float(entry.get("expires_at") or 0) < time.time():
            codes.pop(storage_key, None)
            raise ValueError("邮箱验证码已失效，请重新获取")
        attempts = int(entry.get("attempts") or 0)
        if attempts >= 5:
            codes.pop(storage_key, None)
            raise ValueError("验证码尝试次数过多，请重新获取")
        if not hmac.compare_digest(str(entry.get("digest") or ""), _digest(email, code, settings.api_token, purpose, scope)):
            entry["attempts"] = attempts + 1
            raise ValueError("邮箱验证码错误")
        codes.pop(storage_key, None)
        return email

    with _LOCK:
        if postgres.enabled():
            return postgres.mutate_document("email_verifications", {"codes": {}}, consume)
        data = _read()
        try:
            return consume(data)
        finally:
            _write(data)
