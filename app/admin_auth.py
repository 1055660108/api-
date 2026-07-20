from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time


PASSWORD_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_COOKIE_NAME = "dola_admin_session"
_SESSIONS_LOCK = threading.RLock()
_SESSIONS: dict[str, tuple[float, str]] = {}


def validate_username(username: str) -> str:
    value = str(username or "").strip()
    if len(value) < 3 or len(value) > 64:
        raise ValueError("管理员账号长度必须为 3 到 64 个字符")
    if any(character.isspace() or ord(character) < 33 for character in value):
        raise ValueError("管理员账号不能包含空白或控制字符")
    return value


def validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < 8 or len(value) > 256:
        raise ValueError("管理员密码长度必须为 8 到 256 个字符")
    return value


def hash_password(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = str(encoded or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


def create_session(username: str) -> str:
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    with _SESSIONS_LOCK:
        _prune_sessions(now)
        _SESSIONS[session_id] = (now + SESSION_TTL_SECONDS, username)
    return session_id


def session_username(session_id: str) -> str | None:
    if not session_id:
        return None
    now = time.time()
    with _SESSIONS_LOCK:
        _prune_sessions(now)
        session = _SESSIONS.get(session_id)
        if not session:
            return None
        expires_at, username = session
        _SESSIONS[session_id] = (now + SESSION_TTL_SECONDS, username)
        return username if expires_at > now else None


def delete_session(session_id: str) -> None:
    if not session_id:
        return
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)


def delete_user_sessions(username: str) -> None:
    with _SESSIONS_LOCK:
        for session_id, (_, session_username_value) in list(_SESSIONS.items()):
            if hmac.compare_digest(session_username_value, username):
                _SESSIONS.pop(session_id, None)


def clear_sessions() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


def _prune_sessions(now: float) -> None:
    for session_id, (expires_at, _) in list(_SESSIONS.items()):
        if expires_at <= now:
            _SESSIONS.pop(session_id, None)
