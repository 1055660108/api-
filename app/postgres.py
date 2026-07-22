from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dola_schema_version (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dola_tasks (
    id varchar(32) PRIMARY KEY,
    meta jsonb NOT NULL,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dola_tasks_owner_idx ON dola_tasks ((meta->>'owner_token_hash'));
CREATE INDEX IF NOT EXISTS dola_tasks_status_idx ON dola_tasks ((meta->>'status'));
CREATE INDEX IF NOT EXISTS dola_tasks_created_idx ON dola_tasks ((meta->>'created_at') DESC);
CREATE INDEX IF NOT EXISTS dola_tasks_owner_created_idx ON dola_tasks ((meta->>'owner_token_hash'), (meta->>'created_at') DESC, id DESC);
CREATE INDEX IF NOT EXISTS dola_tasks_status_poll_idx ON dola_tasks ((meta->>'status'), (COALESCE(meta->>'next_result_poll_at', meta->>'submitted_at', meta->>'updated_at')));
CREATE UNIQUE INDEX IF NOT EXISTS dola_tasks_idempotency_idx ON dola_tasks (
    (meta->>'owner_token_hash'),
    (meta->>'request_route'),
    (meta->>'idempotency_hash')
) WHERE COALESCE(meta->>'idempotency_hash', '') <> '';
CREATE TABLE IF NOT EXISTS dola_documents (
    name text PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dola_accounts (
    id varchar(64) PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dola_accounts_platform_enabled_idx ON dola_accounts ((payload->>'platform'), (payload->>'enabled'));
CREATE TABLE IF NOT EXISTS dola_temp_tokens (
    id varchar(64) PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dola_temp_tokens_concurrency_idx ON dola_temp_tokens ((payload->>'concurrency'));
CREATE TABLE IF NOT EXISTS dola_users (
    username_key text PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dola_users_id_idx ON dola_users ((payload->>'id'));
CREATE INDEX IF NOT EXISTS dola_users_token_hash_idx ON dola_users ((payload->>'token_hash'));
CREATE INDEX IF NOT EXISTS dola_users_email_idx ON dola_users ((LOWER(payload->>'email'))) WHERE COALESCE(payload->>'email', '') <> '';
CREATE TABLE IF NOT EXISTS dola_point_transactions (
    id varchar(32) PRIMARY KEY,
    user_id varchar(64) NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dola_point_transactions_user_created_idx ON dola_point_transactions (user_id, created_at DESC, id DESC);
INSERT INTO dola_schema_version(version) VALUES (1) ON CONFLICT (version) DO NOTHING;
INSERT INTO dola_accounts(id, payload)
SELECT item->>'id', item
FROM dola_documents
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(payload->'accounts', '[]'::jsonb)) AS item
WHERE name = 'accounts'
  AND COALESCE(item->>'id', '') <> ''
  AND NOT EXISTS (SELECT 1 FROM dola_schema_version WHERE version = 2)
ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now();
INSERT INTO dola_schema_version(version) VALUES (2) ON CONFLICT (version) DO NOTHING;
INSERT INTO dola_temp_tokens(id, payload)
SELECT item.key, item.value
FROM dola_documents
CROSS JOIN LATERAL jsonb_each(COALESCE(payload->'tokens', '{}'::jsonb)) AS item
WHERE name = 'temp_tokens'
  AND NOT EXISTS (SELECT 1 FROM dola_schema_version WHERE version = 3)
ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now();
INSERT INTO dola_users(username_key, payload)
SELECT item.key, item.value
FROM dola_documents
CROSS JOIN LATERAL jsonb_each(COALESCE(payload->'users', '{}'::jsonb)) AS item
WHERE name = 'users'
  AND NOT EXISTS (SELECT 1 FROM dola_schema_version WHERE version = 3)
ON CONFLICT (username_key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now();
INSERT INTO dola_point_transactions(id, user_id, payload, created_at)
SELECT item->>'id', item->>'user_id', item,
       COALESCE(NULLIF(item->>'created_at', '')::timestamptz, now())
FROM dola_documents
CROSS JOIN LATERAL jsonb_array_elements(COALESCE(payload->'transactions', '[]'::jsonb)) AS item
WHERE name = 'point_transactions'
  AND COALESCE(item->>'id', '') <> ''
  AND COALESCE(item->>'user_id', '') <> ''
  AND NOT EXISTS (SELECT 1 FROM dola_schema_version WHERE version = 3)
ON CONFLICT (id) DO NOTHING;
INSERT INTO dola_schema_version(version) VALUES (3) ON CONFLICT (version) DO NOTHING;
"""


def database_url() -> str:
    return str(os.environ.get("DOLA_DATABASE_URL") or "").strip()


def enabled() -> bool:
    return bool(database_url())


def _driver():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL storage requires psycopg; install requirements.txt") from exc
    return psycopg


_pool = None
_pool_signature = ""
_pool_lock = threading.RLock()
_schema_ready = False


def _connection_pool():
    global _pool, _pool_signature
    url = database_url()
    max_size = max(2, int(os.environ.get("DOLA_DATABASE_POOL_SIZE") or 8))
    signature = f"{url}|{max_size}"
    with _pool_lock:
        if _pool is None or _pool_signature != signature:
            if _pool is not None:
                _pool.close()
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                return None
            _pool = ConnectionPool(url, min_size=1, max_size=max_size, open=True)
            _pool_signature = signature
        return _pool


@contextmanager
def connection() -> Iterator[Any]:
    if not enabled():
        raise RuntimeError("DOLA_DATABASE_URL is not configured")
    pool = _connection_pool()
    if pool is None:
        with _driver().connect(database_url()) as conn:
            yield conn
        return
    with pool.connection() as conn:
        yield conn


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _pool_lock:
        if _schema_ready:
            return
        with connection() as conn:
            conn.execute(SCHEMA_SQL)
        _schema_ready = True


def read_document(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if name == "accounts":
        with connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM dola_accounts ORDER BY payload->>'created_at', id"
            ).fetchall()
        return {"accounts": [dict(row[0]) for row in rows]}
    if name in {"temp_tokens", "users"}:
        table, key_name = ("dola_temp_tokens", "tokens") if name == "temp_tokens" else ("dola_users", "users")
        key_column = "id" if name == "temp_tokens" else "username_key"
        with connection() as conn:
            rows = conn.execute(f"SELECT {key_column}, payload FROM {table} ORDER BY {key_column}").fetchall()
        return {key_name: {str(row[0]): dict(row[1]) for row in rows}}
    if name == "point_transactions":
        with connection() as conn:
            rows = conn.execute("SELECT payload FROM dola_point_transactions ORDER BY created_at, id").fetchall()
        return {"transactions": [dict(row[0]) for row in rows]}
    with connection() as conn:
        row = conn.execute("SELECT payload FROM dola_documents WHERE name = %s", (name,)).fetchone()
    if not row:
        return {} if default is None else dict(default)
    return dict(row[0])


def write_document(name: str, payload: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    if name == "accounts":
        accounts = [item for item in payload.get("accounts", []) if isinstance(item, dict) and str(item.get("id") or "")]
        with connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('dola_accounts', 0))")
            conn.execute("DELETE FROM dola_accounts")
            for account in accounts:
                conn.execute(
                    "INSERT INTO dola_accounts(id, payload) VALUES (%s, %s)",
                    (str(account["id"]), Jsonb(account)),
                )
        return
    if name == "point_transactions":
        entries = payload.get("transactions")
        normalized = entries if isinstance(entries, list) else []
        with connection() as conn:
            conn.execute("DELETE FROM dola_point_transactions")
            for entry in normalized:
                if not isinstance(entry, dict) or not str(entry.get("id") or "") or not str(entry.get("user_id") or ""):
                    continue
                conn.execute(
                    "INSERT INTO dola_point_transactions(id, user_id, payload, created_at) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (str(entry["id"]), str(entry["user_id"]), Jsonb(entry), str(entry.get("created_at") or datetime.now(timezone.utc).isoformat())),
                )
        return
    if name in {"temp_tokens", "users"}:
        table, key_name = ("dola_temp_tokens", "tokens") if name == "temp_tokens" else ("dola_users", "users")
        key_column = "id" if name == "temp_tokens" else "username_key"
        entries = payload.get(key_name)
        normalized = entries if isinstance(entries, dict) else {}
        with connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (table,))
            conn.execute(f"DELETE FROM {table}")
            for entry_id, entry in normalized.items():
                if not isinstance(entry, dict) or not str(entry_id):
                    continue
                conn.execute(
                    f"INSERT INTO {table}({key_column}, payload) VALUES (%s, %s)",
                    (str(entry_id), Jsonb(entry)),
                )
        return
    with connection() as conn:
        conn.execute(
            "INSERT INTO dola_documents(name, payload) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
            (name, Jsonb(payload)),
        )


def mutate_document(name: str, default: dict[str, Any], mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

    if name == "accounts":
        with connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('dola_accounts', 0))")
            rows = conn.execute("SELECT id, payload FROM dola_accounts ORDER BY payload->>'created_at', id FOR UPDATE").fetchall()
            before = {str(row[0]): dict(row[1]) for row in rows}
            payload = {"accounts": [deepcopy(item) for item in before.values()]}
            result = mutator(payload)
            after = {
                str(item.get("id") or ""): item
                for item in payload.get("accounts", [])
                if isinstance(item, dict) and str(item.get("id") or "")
            }
            removed = set(before) - set(after)
            if removed:
                conn.execute("DELETE FROM dola_accounts WHERE id = ANY(%s)", (list(removed),))
            for account_id, account in after.items():
                if before.get(account_id) == account:
                    continue
                conn.execute(
                    "INSERT INTO dola_accounts(id, payload) VALUES (%s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                    (account_id, Jsonb(account)),
                )
            return result
    if name in {"temp_tokens", "users"}:
        table, key_name = ("dola_temp_tokens", "tokens") if name == "temp_tokens" else ("dola_users", "users")
        key_column = "id" if name == "temp_tokens" else "username_key"
        with connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (table,))
            rows = conn.execute(f"SELECT {key_column}, payload FROM {table} ORDER BY {key_column} FOR UPDATE").fetchall()
            before = {str(row[0]): dict(row[1]) for row in rows}
            payload = {key_name: {key: deepcopy(value) for key, value in before.items()}}
            result = mutator(payload)
            raw_after = payload.get(key_name)
            after = {
                str(key): value
                for key, value in (raw_after.items() if isinstance(raw_after, dict) else [])
                if str(key) and isinstance(value, dict)
            }
            removed = set(before) - set(after)
            if removed:
                conn.execute(f"DELETE FROM {table} WHERE {key_column} = ANY(%s)", (list(removed),))
            for entry_id, entry in after.items():
                if before.get(entry_id) == entry:
                    continue
                conn.execute(
                    f"INSERT INTO {table}({key_column}, payload) VALUES (%s, %s) "
                    f"ON CONFLICT ({key_column}) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                    (entry_id, Jsonb(entry)),
                )
            return result
    with connection() as conn:
        conn.execute(
            "INSERT INTO dola_documents(name, payload) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
            (name, Jsonb(default)),
        )
        row = conn.execute("SELECT payload FROM dola_documents WHERE name = %s FOR UPDATE", (name,)).fetchone()
        payload = dict(row[0])
        result = mutator(payload)
        conn.execute(
            "UPDATE dola_documents SET payload = %s, updated_at = now() WHERE name = %s",
            (Jsonb(payload), name),
        )
        return result


def mutate_account(account_id: str, mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        row = conn.execute(
            "SELECT payload FROM dola_accounts WHERE id = %s FOR UPDATE",
            (str(account_id),),
        ).fetchone()
        if not row:
            raise KeyError("account not found")
        account = dict(row[0])
        result = mutator(account)
        conn.execute(
            "UPDATE dola_accounts SET payload = %s, updated_at = now() WHERE id = %s",
            (Jsonb(account), str(account_id)),
        )
        return result


def read_temp_token(token_hash: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT payload FROM dola_temp_tokens WHERE id = %s", (str(token_hash),)).fetchone()
    return dict(row[0]) if row else None


def mutate_temp_token(token_hash: str, mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended('dola_temp_tokens', 0))")
        row = conn.execute(
            "SELECT payload FROM dola_temp_tokens WHERE id = %s FOR UPDATE",
            (str(token_hash),),
        ).fetchone()
        if not row:
            raise KeyError("token not found")
        entry = dict(row[0])
        before = deepcopy(entry)
        result = mutator(entry)
        if entry != before:
            conn.execute(
                "UPDATE dola_temp_tokens SET payload = %s, updated_at = now() WHERE id = %s",
                (Jsonb(entry), str(token_hash)),
            )
        return result


def delete_temp_token(token_hash: str) -> bool:
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended('dola_temp_tokens', 0))")
        row = conn.execute("DELETE FROM dola_temp_tokens WHERE id = %s RETURNING id", (str(token_hash),)).fetchone()
    return row is not None


def rotate_temp_token(token_hash: str, new_hash: str, token: str, updated_at: str) -> dict[str, Any] | None:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended('dola_temp_tokens', 0))")
        row = conn.execute(
            "SELECT payload FROM dola_temp_tokens WHERE id = %s FOR UPDATE",
            (str(token_hash),),
        ).fetchone()
        if not row:
            raise KeyError("token not found")
        replacement = dict(row[0])
        replacement.update(token=str(token), updated_at=str(updated_at))
        inserted = conn.execute(
            "INSERT INTO dola_temp_tokens(id, payload) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
            (str(new_hash), Jsonb(replacement)),
        ).fetchone()
        if not inserted:
            return None
        conn.execute("DELETE FROM dola_temp_tokens WHERE id = %s", (str(token_hash),))
        return replacement


def _user_lookup(field: str, value: str) -> tuple[str, tuple[Any, ...]]:
    normalized = str(value or "")
    if field == "username_key":
        return "username_key = %s", (normalized.casefold(),)
    if field in {"id", "token_hash"}:
        return f"payload->>'{field}' = %s", (normalized,)
    if field == "email":
        return "LOWER(payload->>'email') = %s", (normalized.casefold(),)
    raise ValueError("unsupported user lookup field")


def read_user(field: str, value: str) -> tuple[str, dict[str, Any]] | None:
    condition, params = _user_lookup(field, value)
    with connection() as conn:
        row = conn.execute(
            f"SELECT username_key, payload FROM dola_users WHERE {condition} LIMIT 1",
            params,
        ).fetchone()
    return (str(row[0]), dict(row[1])) if row else None


def mutate_user(field: str, value: str, mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

    condition, params = _user_lookup(field, value)
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended('dola_users', 0))")
        row = conn.execute(
            f"SELECT username_key, payload FROM dola_users WHERE {condition} LIMIT 1 FOR UPDATE",
            params,
        ).fetchone()
        if not row:
            raise KeyError("user not found")
        username_key = str(row[0])
        entry = dict(row[1])
        before = deepcopy(entry)
        result = mutator(entry)
        if entry != before:
            conn.execute(
                "UPDATE dola_users SET payload = %s, updated_at = now() WHERE username_key = %s",
                (Jsonb(entry), username_key),
            )
        return result


def insert_user(username_key: str, entry: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    key = str(username_key or "").casefold()
    email = str(entry.get("email") or "").casefold()
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('dola_users', 0))")
        if email and conn.execute(
            "SELECT 1 FROM dola_users WHERE LOWER(payload->>'email') = %s LIMIT 1",
            (email,),
        ).fetchone():
            raise ValueError("email already exists")
        row = conn.execute(
            "INSERT INTO dola_users(username_key, payload) VALUES (%s, %s) "
            "ON CONFLICT (username_key) DO NOTHING RETURNING username_key",
            (key, Jsonb(entry)),
        ).fetchone()
        if not row:
            raise ValueError("username already exists")


def delete_user(field: str, value: str) -> dict[str, Any] | None:
    condition, params = _user_lookup(field, value)
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended('dola_users', 0))")
        row = conn.execute(
            f"DELETE FROM dola_users WHERE username_key = (SELECT username_key FROM dola_users WHERE {condition} LIMIT 1) RETURNING payload",
            params,
        ).fetchone()
    return dict(row[0]) if row else None


def insert_point_transaction(entry: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    created_at = str(entry.get("created_at") or datetime.now(timezone.utc).isoformat())
    with connection() as conn:
        conn.execute(
            "INSERT INTO dola_point_transactions(id, user_id, payload, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(entry["id"]), str(entry["user_id"]), Jsonb(entry), created_at),
        )


def query_point_transactions(user_id: str, page: int, page_size: int) -> dict[str, Any]:
    with connection() as conn:
        total_row = conn.execute(
            "SELECT count(*) FROM dola_point_transactions WHERE user_id = %s",
            (str(user_id),),
        ).fetchone()
        total = int(total_row[0] if total_row else 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(max(1, int(page)), total_pages)
        rows = conn.execute(
            "SELECT payload FROM dola_point_transactions WHERE user_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (str(user_id), page_size, (current_page - 1) * page_size),
        ).fetchall()
    return {
        "transactions": [dict(row[0]) for row in rows],
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


def claim_available_account(
    platform: str,
    excluded_ids: set[str],
    today: str,
    now: str,
    mutator: Callable[[dict[str, Any]], T],
) -> T | None:
    from psycopg.types.json import Jsonb

    quota_limit = "CASE WHEN COALESCE(payload->>'quota_limit', '') ~ '^[0-9]+$' THEN (payload->>'quota_limit')::integer ELSE 0 END"
    quota_used = "CASE WHEN COALESCE(payload->>'quota_used', '') ~ '^[0-9]+$' THEN (payload->>'quota_used')::integer ELSE 0 END"
    excluded = sorted({str(item) for item in excluded_ids if str(item)})
    conditions = [
        "COALESCE(payload->>'enabled', 'true') = 'true'",
        "COALESCE(payload->>'platform', 'dola') = %s",
        "COALESCE(payload->>'current_task_id', '') = ''",
        "jsonb_typeof(payload->'cookies') = 'array'",
        "jsonb_array_length(payload->'cookies') > 0",
        "COALESCE(payload->>'quota_exhausted_date', '') <> %s",
        f"({quota_limit} = 0 OR {quota_used} < {quota_limit})",
        "(COALESCE(payload->>'cooldown_until', '') = '' OR payload->>'cooldown_until' <= %s)",
    ]
    params: list[Any] = [platform, today, now]
    if excluded:
        conditions.append("NOT (id = ANY(%s))")
        params.append(excluded)
    query = (
        f"SELECT id, payload FROM dola_accounts WHERE {' AND '.join(conditions)} "
        f"ORDER BY CASE WHEN {quota_limit} = 2 AND {quota_used} = 0 THEN 0 ELSE 1 END, "
        f"CASE WHEN {quota_limit} = 0 THEN 1000000 ELSE {quota_limit} - {quota_used} END DESC, "
        f"{quota_used}, COALESCE(payload->>'last_used_at', ''), id "
        "FOR UPDATE SKIP LOCKED LIMIT 1"
    )
    with connection() as conn:
        row = conn.execute(query, tuple(params)).fetchone()
        if not row:
            return None
        account_id = str(row[0])
        account = dict(row[1])
        result = mutator(account)
        conn.execute(
            "UPDATE dola_accounts SET payload = %s, updated_at = now() WHERE id = %s",
            (Jsonb(account), account_id),
        )
        return result


def delete_document(name: str) -> None:
    with connection() as conn:
        if name == "accounts":
            conn.execute("DELETE FROM dola_accounts")
            return
        if name == "temp_tokens":
            conn.execute("DELETE FROM dola_temp_tokens")
            return
        if name == "users":
            conn.execute("DELETE FROM dola_users")
            return
        if name == "point_transactions":
            conn.execute("DELETE FROM dola_point_transactions")
            return
        conn.execute("DELETE FROM dola_documents WHERE name = %s", (name,))


def task_exists(task_id: str) -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 FROM dola_tasks WHERE id = %s", (task_id,)).fetchone() is not None


def create_task(task_id: str, meta: dict[str, Any]) -> bool:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        row = conn.execute(
            "INSERT INTO dola_tasks(id, meta) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
            (task_id, Jsonb(meta)),
        ).fetchone()
    return row is not None


def find_or_create_idempotent_task(task_id: str, meta: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    from psycopg.types.json import Jsonb

    owner_token_hash = str(meta.get("owner_token_hash") or "")
    request_route = str(meta.get("request_route") or "")
    idempotency_hash = str(meta.get("idempotency_hash") or "")
    request_fingerprint = str(meta.get("request_fingerprint") or "")
    lock_scope = f"{owner_token_hash}:{request_route}:{idempotency_hash}"
    with connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_scope,))
        row = conn.execute(
            "SELECT meta FROM dola_tasks "
            "WHERE meta->>'owner_token_hash' = %s "
            "AND meta->>'request_route' = %s "
            "AND meta->>'idempotency_hash' = %s "
            "LIMIT 1 FOR UPDATE",
            (owner_token_hash, request_route, idempotency_hash),
        ).fetchone()
        if row:
            existing = dict(row[0])
            if str(existing.get("request_fingerprint") or "") != request_fingerprint:
                raise ValueError("idempotency key conflicts with a different request")
            return existing, False
        conn.execute("INSERT INTO dola_tasks(id, meta) VALUES (%s, %s)", (task_id, Jsonb(meta)))
        return dict(meta), True


def read_task_part(task_id: str, part: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if part not in {"meta", "result"}:
        raise ValueError("invalid task part")
    with connection() as conn:
        row = conn.execute(f"SELECT {part} FROM dola_tasks WHERE id = %s", (task_id,)).fetchone()
    if not row:
        if part == "meta" and default is None:
            raise FileNotFoundError(task_id)
        return {} if default is None else dict(default)
    return dict(row[0])


def write_task_part(task_id: str, part: str, payload: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    if part not in {"meta", "result"}:
        raise ValueError("invalid task part")
    with connection() as conn:
        cursor = conn.execute(
            f"UPDATE dola_tasks SET {part} = %s, updated_at = now() WHERE id = %s",
            (Jsonb(payload), task_id),
        )
        if cursor.rowcount == 0:
            raise FileNotFoundError(task_id)


def mutate_task_part(task_id: str, part: str, mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

    if part not in {"meta", "result"}:
        raise ValueError("invalid task part")
    with connection() as conn:
        row = conn.execute(f"SELECT {part} FROM dola_tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        if not row:
            raise FileNotFoundError(task_id)
        payload = dict(row[0])
        result = mutator(payload)
        conn.execute(
            f"UPDATE dola_tasks SET {part} = %s, updated_at = now() WHERE id = %s",
            (Jsonb(payload), task_id),
        )
        return result


def claim_task(task_id: str, worker_id: str, owner_token_hash: str, concurrency_limit: int | None, claimed_at: str) -> bool:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        if owner_token_hash and concurrency_limit is not None:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (owner_token_hash,))
        row = conn.execute("SELECT meta FROM dola_tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        if not row:
            raise FileNotFoundError(task_id)
        meta = dict(row[0])
        if str(meta.get("status") or "") != "pending" or bool(meta.get("cancel_requested")):
            return False
        if owner_token_hash and concurrency_limit is not None:
            active = conn.execute(
                "SELECT count(*) FROM dola_tasks WHERE meta->>'owner_token_hash' = %s AND ("
                "meta->>'status' IN ('running', 'submitted') OR ("
                "meta->>'status' = 'success' AND COALESCE(result->>'decoded_main_url', '') = ''))",
                (owner_token_hash,),
            ).fetchone()
            if int(active[0]) >= max(1, int(concurrency_limit)):
                return False
        meta.update(
            status="running",
            worker_id=worker_id,
            started_at=claimed_at,
            claimed_at=claimed_at,
            attempt=max(0, int(meta.get("attempt") or 0)) + 1,
            error="",
            execution_miss_count=0,
            submit_phase="",
            submit_started_at="",
            updated_at=claimed_at,
        )
        conn.execute(
            "UPDATE dola_tasks SET meta = %s, updated_at = now() WHERE id = %s",
            (Jsonb(meta), task_id),
        )
        return True


def list_task_ids() -> list[str]:
    with connection() as conn:
        rows = conn.execute("SELECT id FROM dola_tasks ORDER BY meta->>'created_at' DESC, id DESC").fetchall()
    return [str(row[0]) for row in rows]


def list_task_metas(owner_token_hash: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    query = "SELECT id, meta FROM dola_tasks"
    params: tuple[Any, ...] = ()
    if owner_token_hash is not None:
        query += " WHERE meta->>'owner_token_hash' = %s"
        params = (owner_token_hash,)
    query += " ORDER BY meta->>'created_at' DESC, id DESC"
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [(str(row[0]), dict(row[1])) for row in rows]


def _task_scope_conditions(owner_token_hash: str | None, audience: str | None) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if owner_token_hash is not None:
        conditions.append("meta->>'owner_token_hash' = %s")
        params.append(owner_token_hash)
    if audience in {"admin", "client"}:
        conditions.append(f"COALESCE(meta->>'task_hidden_for_{audience}', 'false') <> 'true'")
    return conditions, params


def query_task_page(
    *,
    owner_token_hash: str | None,
    audience: str,
    page: int,
    page_size: int,
    keyword: str = "",
    status: str = "",
    platform: str = "",
    matching_owner_hashes: list[str] | None = None,
) -> dict[str, Any]:
    scope_conditions, scope_params = _task_scope_conditions(owner_token_hash, audience)
    conditions = list(scope_conditions)
    params = list(scope_params)
    if status and status != "all":
        conditions.append("LOWER(COALESCE(meta->>'status', '')) = %s")
        params.append(status.lower())
    if platform and platform != "all":
        conditions.append("LOWER(COALESCE(meta->>'platform', '')) = %s")
        params.append(platform.lower())
    if keyword:
        pattern = f"%{keyword.lower()}%"
        searchable = ("id", "prompt", "status", "error", "model", "platform", "created_at", "updated_at")
        search_conditions = [
            "LOWER(COALESCE(id, '')) LIKE %s" if field == "id" else f"LOWER(COALESCE(meta->>'{field}', '')) LIKE %s"
            for field in searchable
        ]
        params.extend([pattern] * len(search_conditions))
        owner_hashes = [str(item) for item in (matching_owner_hashes or []) if str(item)]
        if owner_hashes:
            search_conditions.append("meta->>'owner_token_hash' = ANY(%s)")
            params.append(owner_hashes)
        conditions.append(f"({' OR '.join(search_conditions)})")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    scope_where = f" WHERE {' AND '.join(scope_conditions)}" if scope_conditions else ""
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(timezone(timedelta(hours=8)))
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc).isoformat()
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    with connection() as conn:
        total_row = conn.execute(f"SELECT count(*) FROM dola_tasks{where}", tuple(params)).fetchone()
        total = int(total_row[0] if total_row else 0)
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(max(1, page), total_pages)
        rows = conn.execute(
            f"SELECT id, meta FROM dola_tasks{where} ORDER BY meta->>'created_at' DESC, id DESC LIMIT %s OFFSET %s",
            (*params, page_size, (current_page - 1) * page_size),
        ).fetchall()
        stats_row = conn.execute(
            "SELECT count(*), "
            "count(*) FILTER (WHERE meta->>'status' = 'pending'), "
            "count(*) FILTER (WHERE meta->>'status' IN ('running', 'submitted')), "
            "count(*) FILTER (WHERE meta->>'status' = 'success'), "
            "count(*) FILTER (WHERE meta->>'status' IN ('failed', 'canceled')), "
            "count(*) FILTER (WHERE COALESCE(meta->>'finished_at', '') >= %s AND COALESCE(meta->>'finished_at', '') < %s) "
            f"FROM dola_tasks{scope_where}",
            (start_utc, end_utc, *scope_params),
        ).fetchone()
    stats_values = tuple(stats_row or (0, 0, 0, 0, 0, 0))
    return {
        "items": [(str(row[0]), dict(row[1])) for row in rows],
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
        "stats": {
            "total": int(stats_values[0]),
            "pending": int(stats_values[1]),
            "running": int(stats_values[2]),
            "success": int(stats_values[3]),
            "failed": int(stats_values[4]),
            "completed_today": int(stats_values[5]),
        },
    }


def list_task_metas_by_statuses(
    statuses: set[str],
    *,
    platform: str | None = None,
    due_before: str | None = None,
    limit: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    normalized = sorted({str(item) for item in statuses if str(item)})
    if not normalized:
        return []
    conditions = ["meta->>'status' = ANY(%s)"]
    params: list[Any] = [normalized]
    if platform:
        conditions.append("COALESCE(meta->>'platform', 'dola') = %s")
        params.append(platform)
    if due_before:
        conditions.append("COALESCE(meta->>'next_result_poll_at', meta->>'submitted_at', meta->>'updated_at', '') <= %s")
        params.append(due_before)
    query = f"SELECT id, meta FROM dola_tasks WHERE {' AND '.join(conditions)} ORDER BY COALESCE(meta->>'next_result_poll_at', meta->>'submitted_at', meta->>'updated_at', ''), id"
    if limit is not None:
        query += " LIMIT %s"
        params.append(max(1, int(limit)))
    with connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [(str(row[0]), dict(row[1])) for row in rows]


def count_tasks(status: str | None = None) -> int:
    if status:
        query = "SELECT count(*) FROM dola_tasks WHERE meta->>'status' = %s"
        params = (status,)
    else:
        query = "SELECT count(*) FROM dola_tasks"
        params = ()
    with connection() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row[0] if row else 0)


def delete_task(task_id: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM dola_tasks WHERE id = %s", (task_id,))


def clear_all() -> None:
    with connection() as conn:
        conn.execute("TRUNCATE dola_tasks, dola_documents, dola_accounts, dola_temp_tokens, dola_users, dola_point_transactions")
