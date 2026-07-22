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
