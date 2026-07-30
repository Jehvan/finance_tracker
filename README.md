# Finance Tracker

A personal spending + investing tracker. Flask + Postgres (Supabase) backend, React (Vite) PWA frontend.

## 1. Set up your database (Supabase)

1. Go to https://supabase.com and create a free account/project.
2. In your project, go to **Project Settings → Database → Connection string** and copy the
   **URI** (it looks like `postgresql://postgres:[password]@db.xxxxx.supabase.co:5432/postgres`).
   Use the pooled "Transaction" connection string if offered — it's better suited to a
   web app that opens/closes connections per request.
3. That's it — the app creates its own tables automatically the first time it runs
   (see `init_db()` in `app.py`).

## 2. Run the backend locally

```bash
cd backend
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
export DATABASE_URL="your Supabase connection string"   # Windows: set DATABASE_URL=...
python app.py
```

This starts the Flask API on `http://0.0.0.0:5000`.

Optional: for live stock prices, get a free key from https://www.alphavantage.co/support/#api-key
and set `ALPHAVANTAGE_API_KEY` the same way. Crypto prices (via CoinGecko) work with no key
needed — use CoinGecko's *id* as the symbol, e.g. `bitcoin`, not the ticker `BTC`.

## 3. Run the frontend locally

```bash
cd frontend
npm install
npm run dev
```

Starts Vite on `http://0.0.0.0:5173`. To use it from your phone on the same WiFi, open
`http://<your-computer's-local-IP>:5173` in Safari, then Share → Add to Home Screen.

## 4. Deploy to Render

The repo includes `render.yaml`, so the easiest path is:

1. Push this project to a GitHub repo.
2. On Render, click **New → Blueprint**, connect the repo, and Render will read
   `render.yaml` and set up both services (`finance-tracker-api` and
   `finance-tracker-frontend`) automatically.
3. In the Render dashboard, open `finance-tracker-api → Environment` and set:
   - `DATABASE_URL` → your Supabase connection string
   - `ALPHAVANTAGE_API_KEY` → your key (optional, for stock prices)
4. Once the backend deploys, note its URL (e.g. `https://finance-tracker-api.onrender.com`).
   If it doesn't match what's already in `render.yaml`'s `VITE_API_BASE`, update that
   value and redeploy the frontend.
5. Open the frontend's Render URL on your iPhone in Safari → Share → **Add to Home Screen**.

No local computer needs to stay on — everything runs on Render + Supabase.

### If you'd rather set services up manually instead of via Blueprint
- **Backend**: New → Web Service → Runtime: Python → root dir `backend` →
  build command `pip install -r requirements.txt` → start command `gunicorn app:app`.
- **Frontend**: New → Static Site → root dir `frontend` →
  build command `npm install && npm run build` → publish directory `dist` →
  env var `VITE_API_BASE` set to `https://<your-backend-url>/api`.

## Notes

- Render's **free web services spin down after ~15 min of inactivity** and take a few
  seconds to wake back up on the next request — expect a short delay the first time you
  open the app after a while. Data is unaffected by this since it now lives in Supabase,
  not on Render's filesystem.
- Supabase's free tier project pauses after 1 week of inactivity too — opening the app
  occasionally keeps both sides awake, or you can manually unpause it in the Supabase
  dashboard if it happens.
- The frontend talks to whichever API base you configure — locally it falls back to
  `window.location.hostname`, in production it uses the `VITE_API_BASE` env var baked in
  at build time.

