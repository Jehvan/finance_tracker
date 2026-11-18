# Finance Tracker

Single-user personal finance app. Flask API (`backend/`, raw psycopg2 against Supabase Postgres)
+ React/Vite PWA (`frontend/src`). Hosted on Render's free plan, so keep it lightweight: no
background workers, few dependencies, few requests. Spending entry is manual only; never add
bank imports or other external financial integrations.

## Commands

```bash
# backend (Render runs Python 3.12; psycopg2-binary 2.9.9 has no 3.13 wheel)
cd backend && pip install -r requirements-dev.txt
pytest                                    # logic tests only; API tests skip
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/finance_test pytest   # + API tests
COOKIE_SECURE=0 python app.py             # needs DATABASE_URL, SECRET_KEY, ADMIN_PASSWORD_HASH, FRONTEND_ORIGIN

# frontend
cd frontend && npm install && npm run dev  # proxies /api to localhost:5000 (API_PROXY_TARGET to change)
npm run build                              # the only frontend check (no tests or lint yet)
```

API tests TRUNCATE tables: `TEST_DATABASE_URL` must be a throwaway local DB, never Supabase.

## Architecture

- `backend/app.py`: every route, auth (`@require_auth` sets `g.user_id` from the JWT `sub`) and `init_db()`.
  Browser auth is an HttpOnly `ft_session` cookie (SameSite=Strict, Path=/api); `Authorization:
  Bearer` still works for tests/non-browser clients. Login is rate limited via `login_failures`.
- `backend/recurring.py`: pure Subscriptions & Bills logic (due-date advancing, cost
  normalization, validation). No Flask or DB imports, so keep new logic there.
- `backend/push.py`: web push (subscription validation with a push-service host allowlist, VAPID,
  sending without redirects). Routes `/api/push/*` in app.py; `/api/push/run-daily` is called by
  `.github/workflows/bill-reminders.yml` with `X-Cron-Secret` (404 if `PUSH_CRON_SECRET` unset).
  Push is off unless `VAPID_PRIVATE_KEY` is set. Digest text lives in `rc.build_digest()`.
- `backend/migrations/NNN_name.sql`: schema changes. Must be idempotent (`IF NOT EXISTS`);
  `init_db()` applies all of them on every startup, and they can be pasted into Supabase.
- `frontend/src/App.jsx`: tabs (Spending, Investing, Bills; bottom tab bar) and the login. Visual
  design is the aubergine/gold/rose theme in `index.css` (tokens on `:root`; gold = money in /
  primary action, rose = money out / overdue; Unbounded for figures, Golos Text for UI). `shared.jsx` holds
  `API_BASE`, `authFetch`, `Money` and date helpers; `recurringApi.js` holds bill API calls;
  `reminders.js` holds in-app reminder logic; `push.js` + `PushSettings.jsx` turn device
  notifications on/off; `src/sw.js` is the service worker (vite-plugin-pwa `injectManifest`).

## Gotchas

- `transactions` stores MKD only; EUR is converted at the fixed peg `EUR_TO_MKD_RATE = 61.5`,
  defined in both `shared.jsx` and `recurring.py`. Keep the two in sync.
- `transactions.date` is TEXT `YYYY-MM-DD`; month filters use `LIKE 'YYYY-MM%'`.
- NUMERIC comes back as float (global `DEC2FLOAT` in app.py); use `rc.to_decimal()` for money math.
- Dates: the frontend uses device-local dates; the backend's "today" is `APP_TIMEZONE`
  (default Europe/Skopje), not UTC.
- Only `recurring_payments` is per-user (`user_id`). Every query on it must filter by
  `id AND user_id = g.user_id` and return 404 for other users' rows. `transactions`/`holdings`
  are global (one login).
- Mark-paid must send the `due_date` being paid; that, the row lock and the unique index on
  `transactions(recurring_payment_id, paid_for_due_date)` prevent duplicate expenses.
- All tables have RLS enabled with no policies. The app connects as table owner (bypasses RLS);
  this only blocks Supabase's public REST API. Don't add policies or `FORCE` RLS.
- The API must stay same-origin with the frontend (`/api`: Render rewrite in `render.yaml`, Vite
  proxy locally). A cross-origin API would make the cookie third-party, which Safari/iOS blocks
  (symptom: login succeeds, then you're bounced straight back to sign-in). `shared.jsx` therefore
  ignores any absolute `VITE_API_BASE`.
  Never store the token in localStorage. Cookie-authenticated writes need the
  `X-Requested-With` header (`CSRF_HEADERS` in shared.jsx); the backend returns 403 without it.
- Push notifications must never include amounts (they show on the lock screen), and the server
  may only POST to allowlisted push-service hosts (SSRF). Daily runs claim each device for the
  day before sending, so repeats don't double-send.
- Playwright's default headless shell reports notifications as "denied"; use
  `chromium.launch({ channel: "chromium" })` to test push UI.
- The frontend never talks to Supabase and must never receive Supabase credentials
  (`VITE_*` vars end up in the public bundle).

## Workflow

Before finishing any feature or phase, run the secure-coding-check skill.
