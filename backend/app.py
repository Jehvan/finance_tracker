import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import jwt
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from werkzeug.security import check_password_hash

# NUMERIC columns come back from psycopg2 as Decimal by default, which
# Flask's jsonify can't serialize — cast them to float on the way out instead.
DEC2FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values,
    "DEC2FLOAT",
    lambda value, curs: float(value) if value is not None else None,
)
psycopg2.extensions.register_type(DEC2FLOAT)

DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase connection string
PRICE_CACHE_SECONDS = 15 * 60  # refresh crypto prices at most every 15 min

SECRET_KEY = os.environ["SECRET_KEY"]  # random string, used to sign login tokens
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]  # generate with werkzeug.security.generate_password_hash
TOKEN_TTL_DAYS = 30

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

app = Flask(__name__)
CORS(app, origins=[os.environ["FRONTEND_ORIGIN"]])  # only the deployed frontend may call this API


# ---------- Auth ----------

def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing bearer token"}), 401
        token = auth_header.removeprefix("Bearer ").strip()
        try:
            jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.PyJWTError:
            return jsonify({"error": "invalid or expired token"}), 401
        return view(*args, **kwargs)

    return wrapped


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "")
    password = data.get("password", "")
    if username != ADMIN_USERNAME or not check_password_hash(ADMIN_PASSWORD_HASH, password):
        return jsonify({"error": "invalid username or password"}), 401

    token = jwt.encode(
        {
            "sub": username,
            "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        },
        SECRET_KEY,
        algorithm="HS256",
    )
    return jsonify({"token": token})


# ---------- DB setup ----------

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_numeric_column(cur, table, column):
    """Migrate a legacy REAL (float4) money/quantity column to NUMERIC(12,2).

    REAL accumulates rounding error across SUM()s (drifts by cents on real
    totals), so money and quantity columns should be exact NUMERIC. Only
    alters the column if it isn't already numeric, so this is cheap and
    idempotent on every startup.
    """
    cur.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cur.fetchone()
    if row and row[0] == "real":
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE NUMERIC(12,2)")


MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def run_migrations(cur):
    """Apply every idempotent .sql file in migrations/, in filename order.

    Each file is written to be safely re-runnable (IF NOT EXISTS everywhere),
    so applying them on every startup is cheap and needs no tracking table.
    The same files can be pasted into the Supabase SQL editor by hand.
    """
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if name.endswith(".sql"):
            with open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8") as f:
                cur.execute(f.read())


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            amount NUMERIC(12,2) NOT NULL,
            category TEXT NOT NULL,
            note TEXT,
            type TEXT NOT NULL CHECK(type IN ('expense', 'income'))
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL CHECK(asset_type IN ('crypto', 'stock')),
            quantity NUMERIC(18,8) NOT NULL,
            avg_buy_price NUMERIC(12,2) NOT NULL,
            purchase_date TEXT
        );

        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT PRIMARY KEY,
            price NUMERIC(18,8) NOT NULL,
            last_updated BIGINT NOT NULL
        );
        """
    )
    ensure_numeric_column(cur, "transactions", "amount")
    ensure_numeric_column(cur, "holdings", "quantity")
    ensure_numeric_column(cur, "holdings", "avg_buy_price")
    ensure_numeric_column(cur, "price_cache", "price")
    run_migrations(cur)
    conn.commit()
    cur.close()
    conn.close()


# ---------- Validation helpers ----------

def parse_positive_number(value, field):
    """Parse a strictly-positive, finite number from user input, or raise ValueError."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(n):
        raise ValueError(f"{field} must be a finite number")
    if n <= 0:
        raise ValueError(f"{field} must be greater than 0")
    return n


def valid_month_param():
    """Read and validate the `month` query param, or None if absent. Returns
    (month, error_response) — error_response is None when valid."""
    month = request.args.get("month")
    if month is not None and not MONTH_RE.match(month):
        return None, (jsonify({"error": "month must be in YYYY-MM format"}), 400)
    return month, None


# ---------- Transactions ----------

@app.route("/api/transactions", methods=["GET"])
@require_auth
def list_transactions():
    month, err = valid_month_param()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    if month:
        cur.execute(
            "SELECT * FROM transactions WHERE date LIKE %s ORDER BY date DESC, id DESC",
            (f"{month}%",),
        )
    else:
        cur.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC")
    return jsonify(cur.fetchall())


@app.route("/api/categories", methods=["GET"])
@require_auth
def list_categories():
    """All-time distinct categories/notes, for autocomplete — independent of
    whatever month/range is currently being viewed."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT DISTINCT category FROM transactions ORDER BY category")
    categories = [row["category"] for row in cur.fetchall()]
    cur.execute(
        "SELECT DISTINCT note FROM transactions WHERE note IS NOT NULL AND note <> '' ORDER BY note"
    )
    notes = [row["note"] for row in cur.fetchall()]
    return jsonify({"categories": categories, "notes": notes})


@app.route("/api/transactions", methods=["POST"])
@require_auth
def add_transaction():
    data = request.get_json(force=True)
    required = ["amount", "category", "type", "date"]
    if not all(k in data and data[k] != "" for k in required):
        return jsonify({"error": "amount, category, type, and date are required"}), 400
    if data["type"] not in ("expense", "income"):
        return jsonify({"error": "type must be 'expense' or 'income'"}), 400

    try:
        amount = parse_positive_number(data["amount"], "amount")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    category = str(data["category"]).strip()
    if not category:
        return jsonify({"error": "category is required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO transactions (date, amount, category, note, type) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (data["date"], amount, category, str(data.get("note", "")).strip(), data["type"]),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
@require_auth
def delete_transaction(tx_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
    db.commit()
    return "", 204


@app.route("/api/summary", methods=["GET"])
@require_auth
def summary():
    """Spend and income by category, plus totals — for one month, or all time."""
    month, err = valid_month_param()
    if err:
        return err
    db = get_db()
    cur = db.cursor()
    all_time = request.args.get("range") == "all"
    month = month or datetime.now().strftime("%Y-%m")

    def by_category_for(tx_type):
        if all_time:
            cur.execute(
                """
                SELECT category, SUM(amount) as total
                FROM transactions
                WHERE type = %s
                GROUP BY category
                ORDER BY total DESC
                """,
                (tx_type,),
            )
        else:
            cur.execute(
                """
                SELECT category, SUM(amount) as total
                FROM transactions
                WHERE type = %s AND date LIKE %s
                GROUP BY category
                ORDER BY total DESC
                """,
                (tx_type, f"{month}%"),
            )
        return cur.fetchall()

    by_category = by_category_for("expense")
    income_by_category = by_category_for("income")

    if all_time:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) as spent,
                COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) as earned
            FROM transactions
            """
        )
    else:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'expense' AND date LIKE %s THEN amount END), 0) as spent,
                COALESCE(SUM(CASE WHEN type = 'income' AND date LIKE %s THEN amount END), 0) as earned
            FROM transactions
            """,
            (f"{month}%", f"{month}%"),
        )
    totals = cur.fetchone()

    return jsonify(
        {
            "by_category": by_category,
            "income_by_category": income_by_category,
            "spent": totals["spent"],
            "earned": totals["earned"],
            "month": month,
            "range": "all" if all_time else "month",
        }
    )


# ---------- Holdings + prices ----------

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE = 9, 30  # NYSE/Nasdaq open, ET


def last_market_open(at):
    """The most recent market-open timestamp at or before `at` (ET)."""
    open_today = at.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
    return open_today if at >= open_today else open_today - timedelta(days=1)


def is_fresh(row, asset_type, now):
    """Crypto trades around the clock, so it uses a short rolling cache.
    Stocks only need a new price once per trading day, refreshed at most
    once after each day's market open (keeps well under Alpha Vantage's
    free-tier daily request limit)."""
    if not row:
        return False
    if asset_type == "crypto":
        return now - row["last_updated"] < PRICE_CACHE_SECONDS
    return row["last_updated"] >= last_market_open(datetime.now(MARKET_TZ)).timestamp()


def fetch_crypto_prices(symbols):
    """Batch-fetch every stale crypto symbol in a single CoinGecko call."""
    if not symbols:
        return {}
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(s.lower() for s in symbols), "vs_currencies": "usd"},
            timeout=10,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return {}
    return {s: data.get(s.lower(), {}).get("usd") for s in symbols}


def fetch_stock_price(symbol):
    """Alpha Vantage's free tier only supports one symbol per request."""
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "demo")
    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "GLOBAL_QUOTE", "symbol": symbol.upper(), "apikey": api_key},
            timeout=10,
        )
        data = resp.json()
        return float(data.get("Global Quote", {}).get("05. price", 0)) or None
    except (requests.RequestException, ValueError, KeyError):
        return None


@app.route("/api/holdings", methods=["GET"])
@require_auth
def list_holdings():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM holdings")
    rows = cur.fetchall()
    now = int(time.time())

    symbols = sorted({h["symbol"] for h in rows})
    cur.execute("SELECT * FROM price_cache WHERE symbol = ANY(%s)", (symbols,)) if symbols else None
    cached = {row["symbol"]: row for row in (cur.fetchall() if symbols else [])}

    stale_crypto = sorted({h["symbol"] for h in rows if h["asset_type"] == "crypto" and not is_fresh(cached.get(h["symbol"]), "crypto", now)})
    stale_stocks = sorted({h["symbol"] for h in rows if h["asset_type"] == "stock" and not is_fresh(cached.get(h["symbol"]), "stock", now)})

    fresh_prices = fetch_crypto_prices(stale_crypto)
    for sym in stale_stocks:
        fresh_prices[sym] = fetch_stock_price(sym)

    for symbol, price in fresh_prices.items():
        if price is None:
            continue
        cur.execute(
            """
            INSERT INTO price_cache (symbol, price, last_updated) VALUES (%s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET price = EXCLUDED.price, last_updated = EXCLUDED.last_updated
            """,
            (symbol, price, now),
        )
    if any(p is not None for p in fresh_prices.values()):
        db.commit()

    def price_for(symbol):
        fresh = fresh_prices.get(symbol)
        if fresh is not None:
            return fresh
        row = cached.get(symbol)
        return row["price"] if row else None

    result = []
    for h in rows:
        h = dict(h)
        price = price_for(h["symbol"])
        h["current_price"] = price
        h["current_value"] = price * h["quantity"] if price is not None else None
        h["cost_basis"] = h["avg_buy_price"] * h["quantity"]
        h["gain_loss"] = h["current_value"] - h["cost_basis"] if price is not None else None
        result.append(h)
    return jsonify(result)


@app.route("/api/holdings", methods=["POST"])
@require_auth
def add_holding():
    data = request.get_json(force=True)
    required = ["symbol", "asset_type", "quantity", "avg_buy_price"]
    if not all(k in data and data[k] != "" for k in required):
        return jsonify({"error": "symbol, asset_type, quantity, avg_buy_price are required"}), 400
    if data["asset_type"] not in ("crypto", "stock"):
        return jsonify({"error": "asset_type must be 'crypto' or 'stock'"}), 400

    try:
        quantity = parse_positive_number(data["quantity"], "quantity")
        avg_buy_price = parse_positive_number(data["avg_buy_price"], "avg_buy_price")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    symbol = str(data["symbol"]).strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO holdings (symbol, asset_type, quantity, avg_buy_price, purchase_date) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (symbol, data["asset_type"], quantity, avg_buy_price, data.get("purchase_date")),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/holdings/<int:holding_id>", methods=["DELETE"])
@require_auth
def delete_holding(holding_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM holdings WHERE id = %s", (holding_id,))
    db.commit()
    return "", 204


init_db()  # runs on import too, so gunicorn (used in production) creates the tables

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
