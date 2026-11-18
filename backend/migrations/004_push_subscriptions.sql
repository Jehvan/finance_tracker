-- 004: Web push subscriptions (one row per browser/device that turned reminders on).
-- Idempotent.

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id               SERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL CHECK (char_length(user_id) BETWEEN 1 AND 150),
    -- The browser's push service URL. Unique: one browser = one subscription.
    endpoint         TEXT NOT NULL UNIQUE CHECK (char_length(endpoint) <= 1000),
    p256dh           TEXT NOT NULL CHECK (char_length(p256dh) <= 200),
    auth             TEXT NOT NULL CHECK (char_length(auth) <= 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Day the daily digest last went to this device; makes the daily run
    -- safe to repeat (a retry or double trigger sends nothing new).
    last_notified_on DATE
);

CREATE INDEX IF NOT EXISTS push_subscriptions_user_idx ON push_subscriptions (user_id);

ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY;
