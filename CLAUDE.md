# Finance Tracker

Flask backend (`backend/app.py`, raw psycopg2 against Supabase Postgres) + React/Vite PWA
frontend (`frontend/src`). Hosted on Render's free plan — keep things lightweight (no
background workers, minimal dependencies).

- Schema changes go in `backend/migrations/NNN_name.sql`, written to be idempotent; they are
  applied on startup by `init_db()` and can also be pasted into the Supabase SQL editor.
- The frontend never talks to Supabase directly and must never receive Supabase credentials.

Before finishing any feature or phase, run the secure-coding-check skill.
