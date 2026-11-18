-- 003: Failed sign-in attempts, for /api/login rate limiting.
-- Rows older than the 15-minute window are deleted by the app as it goes,
-- so this table stays tiny. Idempotent.

CREATE TABLE IF NOT EXISTS login_failures (
    id        BIGSERIAL PRIMARY KEY,
    key       TEXT NOT NULL,            -- "user:<username>" or "*" (all attempts)
    failed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS login_failures_key_time_idx ON login_failures (key, failed_at);

-- Same as the other tables: no access through Supabase's public REST API.
ALTER TABLE login_failures ENABLE ROW LEVEL SECURITY;
