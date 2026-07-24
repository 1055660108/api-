from __future__ import annotations

import hashlib
import secrets
import threading
import time


CLIENT_SESSION_COOKIE_NAME = "dola_client_session"
CLIENT_SESSION_TTL_SECONDS = 12 * 60 * 60
_SESSION_PREFIX = "dola:web-session:"
_SESSION_LIMIT = 50_000
_SESSIONS_LOCK = threading.RLock()
_SESSIONS: dict[str, tuple[float, str]] = {}


def _redis_client():
    try:
        from .task_queue import get_task_queue

        return getattr(get_task_queue(), "client", None)
    except Exception:
        return None


def _redis_key(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()
    return f"{_SESSION_PREFIX}{digest}"


def _prune_sessions(now: float) -> None:
    for session_id, (expires_at, _) in list(_SESSIONS.items()):
        if expires_at <= now:
            _SESSIONS.pop(session_id, None)
    while len(_SESSIONS) > _SESSION_LIMIT:
        _SESSIONS.pop(next(iter(_SESSIONS)))


def create_client_session(token_hash: str) -> str:
    session_id = secrets.token_urlsafe(32)
    client = _redis_client()
    if client is not None:
        try:
            client.setex(_redis_key(session_id), CLIENT_SESSION_TTL_SECONDS, str(token_hash or ""))
            return session_id
        except Exception:
            pass
    now = time.time()
    with _SESSIONS_LOCK:
        _prune_sessions(now)
        _SESSIONS[session_id] = (now + CLIENT_SESSION_TTL_SECONDS, str(token_hash or ""))
    return session_id


def client_session_token_hash(session_id: str) -> str | None:
    if not session_id:
        return None
    client = _redis_client()
    if client is not None:
        try:
            value = client.get(_redis_key(session_id))
            if value:
                client.expire(_redis_key(session_id), CLIENT_SESSION_TTL_SECONDS)
                return value.decode("utf-8") if isinstance(value, bytes) else str(value)
            return None
        except Exception:
            pass
    now = time.time()
    with _SESSIONS_LOCK:
        _prune_sessions(now)
        session = _SESSIONS.get(session_id)
        if not session or session[0] <= now:
            return None
        _SESSIONS[session_id] = (now + CLIENT_SESSION_TTL_SECONDS, session[1])
        return session[1]


def delete_client_session(session_id: str) -> None:
    if not session_id:
        return
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)
    client = _redis_client()
    if client is not None:
        try:
            client.delete(_redis_key(session_id))
        except Exception:
            pass


def clear_client_sessions() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()
