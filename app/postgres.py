from __future__ import annotations

import os
import threading
from contextlib import contextmanager
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
INSERT INTO dola_schema_version(version) VALUES (1) ON CONFLICT (version) DO NOTHING;
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
    with connection() as conn:
        row = conn.execute("SELECT payload FROM dola_documents WHERE name = %s", (name,)).fetchone()
    if not row:
        return {} if default is None else dict(default)
    return dict(row[0])


def write_document(name: str, payload: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    with connection() as conn:
        conn.execute(
            "INSERT INTO dola_documents(name, payload) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
            (name, Jsonb(payload)),
        )


def mutate_document(name: str, default: dict[str, Any], mutator: Callable[[dict[str, Any]], T]) -> T:
    from psycopg.types.json import Jsonb

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


def delete_document(name: str) -> None:
    with connection() as conn:
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
        conn.execute("TRUNCATE dola_tasks, dola_documents")
