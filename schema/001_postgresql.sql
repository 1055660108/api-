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
