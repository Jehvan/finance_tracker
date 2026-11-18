-- 002: Turn on row-level security for the pre-existing tables.
--
-- The backend connects as the owner of these tables, and table owners bypass
-- RLS, so the app behaves exactly as before. What this blocks is Supabase's
-- auto-generated REST API: with RLS on and no policies, the anon/authenticated
-- roles (i.e. anyone holding the project's anon key) can no longer read or
-- write spending, holdings or the price cache.
-- Idempotent: enabling RLS on a table that already has it is a no-op.

ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE holdings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_cache  ENABLE ROW LEVEL SECURITY;
