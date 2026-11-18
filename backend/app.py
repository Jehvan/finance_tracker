import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import jwt
import psycopg2
import psycopg2.errors
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from psycopg2 import sql
from werkzeug.security import check_password_hash

import recurring as rc

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

# "Today" for due dates/overdue checks. Render runs in UTC, so without this a
# bill due today would flip to overdue at 1-2am local time.
APP_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Europe/Skopje"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # every request here is a small JSON body
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
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.PyJWTError:
            return jsonify({"error": "invalid or expired token"}), 401
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            return jsonify({"error": "invalid or expired token"}), 401
        g.user_id = sub  # owner key for per-user data (recurring payments)
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


# ---------- Subscriptions & Bills (recurring payments) ----------
#
# Every query below filters on BOTH id and user_id = g.user_id (the JWT `sub`).
# A record owned by someone else is indistinguishable from a missing one (404),
# so ids can't be probed. Pure logic (dates, costs, validation) is in recurring.py.

def app_today():
    return datetime.now(APP_TZ).date()


def json_body():
    """The request's JSON object, or (None, 400 response) if it isn't one."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "request body must be a JSON object"}), 400)
    return data, None


def not_found():
    return jsonify({"error": "recurring payment not found"}), 404


def serialize_recurring(row, today):
    item = {k: v for k, v in row.items() if k != "user_id"}
    item["next_due_date"] = row["next_due_date"].isoformat()
    item["created_at"] = row["created_at"].isoformat()
    item["days_until_due"] = (row["next_due_date"] - today).days
    item["status"] = rc.due_status(row["next_due_date"], today, row["reminder_days_before"], row["is_active"])
    item["monthly_cost"] = float(rc.quantize(rc.monthly_cost(row["amount"], row["billing_cycle"])))
    return item


def fetch_owned_recurring(cur, item_id, for_update=False):
    query = "SELECT * FROM recurring_payments WHERE id = %s AND user_id = %s"
    cur.execute(query + (" FOR UPDATE" if for_update else ""), (item_id, g.user_id))
    return cur.fetchone()


@app.route("/api/recurring", methods=["GET"])
@require_auth
def list_recurring():
    cur = get_db().cursor()
    cur.execute(
        "SELECT * FROM recurring_payments WHERE user_id = %s ORDER BY is_active DESC, next_due_date, id",
        (g.user_id,),
    )
    today = app_today()
    return jsonify([serialize_recurring(r, today) for r in cur.fetchall()])


@app.route("/api/recurring", methods=["POST"])
@require_auth
def create_recurring():
    data, err = json_body()
    if err:
        return err
    try:
        fields = rc.clean_payload(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    fields["user_id"] = g.user_id  # always from the token, never from the body
    columns = list(fields)  # validated allowlist keys only
    query = sql.SQL("INSERT INTO recurring_payments ({}) VALUES ({}) RETURNING *").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
    )
    db = get_db()
    cur = db.cursor()
    cur.execute(query, [fields[c] for c in columns])
    row = cur.fetchone()
    db.commit()
    return jsonify(serialize_recurring(row, app_today())), 201


@app.route("/api/recurring/<int:item_id>", methods=["GET"])
@require_auth
def get_recurring(item_id):
    row = fetch_owned_recurring(get_db().cursor(), item_id)
    if not row:
        return not_found()
    return jsonify(serialize_recurring(row, app_today()))


@app.route("/api/recurring/<int:item_id>", methods=["PUT", "PATCH"])
@require_auth
def update_recurring(item_id):
    """Partial update: only the fields present in the body change."""
    data, err = json_body()
    if err:
        return err
    try:
        fields = rc.clean_payload(data, partial=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    columns = list(fields)
    query = sql.SQL("UPDATE recurring_payments SET {} WHERE id = %s AND user_id = %s RETURNING *").format(
        sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns)
    )
    db = get_db()
    cur = db.cursor()
    cur.execute(query, [fields[c] for c in columns] + [item_id, g.user_id])
    row = cur.fetchone()
    if not row:
        db.rollback()
        return not_found()
    db.commit()
    return jsonify(serialize_recurring(row, app_today()))


@app.route("/api/recurring/<int:item_id>", methods=["DELETE"])
@require_auth
def delete_recurring(item_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM recurring_payments WHERE id = %s AND user_id = %s", (item_id, g.user_id))
    if cur.rowcount == 0:
        db.rollback()
        return not_found()
    db.commit()
    return "", 204


@app.route("/api/recurring/upcoming", methods=["GET"])
@require_auth
def upcoming_recurring():
    """Active payments due within the next N days (default 7), overdue ones included."""
    raw = request.args.get("days", "7")
    if not re.fullmatch(r"\d{1,3}", raw) or int(raw) > 365:
        return jsonify({"error": "days must be a whole number from 0 to 365"}), 400
    days = int(raw)
    today = app_today()
    cur = get_db().cursor()
    cur.execute(
        """
        SELECT * FROM recurring_payments
        WHERE user_id = %s AND is_active AND next_due_date <= %s
        ORDER BY next_due_date, id
        """,
        (g.user_id, today + timedelta(days=days)),
    )
    items = [serialize_recurring(r, today) for r in cur.fetchall()]
    return jsonify(
        {
            "today": today.isoformat(),
            "days": days,
            "count": len(items),
            "overdue_count": sum(1 for i in items if i["status"] == "overdue"),
            "items": items,
        }
    )


@app.route("/api/recurring/summary", methods=["GET"])
@require_auth
def recurring_summary():
    cur = get_db().cursor()
    cur.execute(
        "SELECT id, name, amount, currency, billing_cycle, category, is_active "
        "FROM recurring_payments WHERE user_id = %s",
        (g.user_id,),
    )
    return jsonify(rc.summarize(cur.fetchall()))


@app.route("/api/recurring/<int:item_id>/mark-paid", methods=["POST"])
@require_auth
def mark_recurring_paid(item_id):
    """Log one payment as a normal expense and advance next_due_date one cycle.

    Replay-safe: the client must send the `due_date` it is paying. The row is
    locked (FOR UPDATE), and the payment only goes through if that date is
    still the current next_due_date, so a double-tap, retry or replayed
    request gets 409 instead of a second expense. The unique index on
    (recurring_payment_id, paid_for_due_date) backs this up at the DB level.
    """
    data, err = json_body()
    if err:
        return err
    try:
        due = rc.parse_date(data.get("due_date"), "due_date")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    cur = db.cursor()
    today = app_today()
    row = fetch_owned_recurring(cur, item_id, for_update=True)
    if not row:
        db.rollback()
        return not_found()
    if not row["is_active"]:
        db.rollback()
        return jsonify({"error": "this payment is paused; resume it before marking it paid"}), 409
    if row["next_due_date"] != due:
        db.rollback()
        return jsonify(
            {"error": "already marked paid for that due date", "item": serialize_recurring(row, today)}
        ), 409

    amount_base = rc.quantize(rc.to_base_currency(row["amount"], row["currency"]))
    note = row["name"]
    if row["currency"] != rc.BASE_CURRENCY:
        note += f" (€{rc.quantize(row['amount'])})"
    new_due = rc.advance_due_date(due, row["billing_cycle"], row["billing_anchor_day"])

    try:
        cur.execute(
            """
            INSERT INTO transactions (date, amount, category, note, type, recurring_payment_id, paid_for_due_date)
            VALUES (%s, %s, %s, %s, 'expense', %s, %s) RETURNING id
            """,
            (today.isoformat(), amount_base, rc.EXPENSE_CATEGORY[row["category"]], note, item_id, due),
        )
        tx_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE recurring_payments SET next_due_date = %s WHERE id = %s AND user_id = %s RETURNING *",
            (new_due, item_id, g.user_id),
        )
        updated = cur.fetchone()
        db.commit()
    except psycopg2.errors.UniqueViolation:
        db.rollback()
        return jsonify({"error": "already marked paid for that due date"}), 409

    return jsonify({"transaction_id": tx_id, "item": serialize_recurring(updated, today)}), 201

init_db()  # runs on import too, so gunicorn (used in production) creates the tables

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
