-- 001: Subscriptions & Bills (recurring payments)
--
-- Idempotent: safe to paste into the Supabase SQL editor AND safe to re-run.
-- The backend also applies this file on startup from init_db(), the same way
-- it already creates its other tables, so a manual run is optional.
-- Requires the existing `transactions` table (created by init_db()).

CREATE TABLE IF NOT EXISTS recurring_payments (
    id                   SERIAL PRIMARY KEY,
    -- Owner = the JWT `sub` of the logged-in user. Every query filters on it.
    user_id              TEXT NOT NULL CHECK (char_length(user_id) BETWEEN 1 AND 150),
    name                 TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 100),
    amount               NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    currency             TEXT NOT NULL DEFAULT 'MKD' CHECK (currency IN ('MKD', 'EUR')),
    billing_cycle        TEXT NOT NULL CHECK (billing_cycle IN ('weekly', 'monthly', 'quarterly', 'yearly')),
    next_due_date        DATE NOT NULL,
    -- Day-of-month the bill is "really" due on. Lets Jan 31 -> Feb 28 -> Mar 31
    -- instead of drifting to the 28th forever after one short month.
    billing_anchor_day   SMALLINT NOT NULL CHECK (billing_anchor_day BETWEEN 1 AND 31),
    category             TEXT NOT NULL DEFAULT 'subscription'
                         CHECK (category IN ('subscription', 'utility', 'rent', 'phone', 'other')),
    -- Free-text label like "Visa" or "cash". At most 4 digits in total, so a
    -- card number can never be stored ("Visa ••1234" is fine).
    payment_method_label TEXT CHECK (
                             payment_method_label IS NULL OR (
                                 char_length(payment_method_label) <= 40
                                 AND char_length(regexp_replace(payment_method_label, '[^0-9]', '', 'g')) <= 4
                             )
                         ),
    reminder_days_before SMALLINT NOT NULL DEFAULT 3 CHECK (reminder_days_before BETWEEN 0 AND 60),
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    notes                TEXT CHECK (notes IS NULL OR char_length(notes) <= 500),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Serves the list, /upcoming and /summary (all are "this user's rows, by due date").
CREATE INDEX IF NOT EXISTS recurring_payments_user_due_idx
    ON recurring_payments (user_id, next_due_date);

-- The backend connects as the table owner, which bypasses RLS, so this changes
-- nothing for the app. With RLS on and no policies, Supabase's auto-generated
-- REST API (anon/authenticated roles) can't read or write this table at all.
ALTER TABLE recurring_payments ENABLE ROW LEVEL SECURITY;

-- Link spending entries created by "mark paid" back to their recurring payment.
-- ON DELETE SET NULL: deleting a subscription keeps its past expenses.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS recurring_payment_id INTEGER
        REFERENCES recurring_payments(id) ON DELETE SET NULL;
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS paid_for_due_date DATE;

-- Replay guard: one expense per (recurring payment, billing period). A repeated
-- or concurrent mark-paid for the same due date fails here even if the
-- application-level check were somehow bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS transactions_recurring_period_uniq
    ON transactions (recurring_payment_id, paid_for_due_date)
    WHERE recurring_payment_id IS NOT NULL;
