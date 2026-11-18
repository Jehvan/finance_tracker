-- 005: Bill reminders default to 7 days before the due date (was 3), and track
-- the one-time push heads-up per due date. Idempotent.

ALTER TABLE recurring_payments ALTER COLUMN reminder_days_before SET DEFAULT 7;

-- Due date the push heads-up was last sent for; a new due date (mark paid,
-- edit) re-arms it. See recurring.needs_notification().
ALTER TABLE recurring_payments ADD COLUMN IF NOT EXISTS heads_up_sent_for DATE;

-- One-time data changes. Migrations run on every startup, so anything that is
-- not naturally idempotent is recorded here and applied only once.
CREATE TABLE IF NOT EXISTS app_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_migrations ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app_migrations WHERE name = '005_existing_reminders_to_7') THEN
        -- Bills still on the old 3-day default move to 7; any other value was a deliberate choice.
        UPDATE recurring_payments SET reminder_days_before = 7 WHERE reminder_days_before = 3;
        INSERT INTO app_migrations (name) VALUES ('005_existing_reminders_to_7');
    END IF;
END
$$;
