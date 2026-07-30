import os
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, request
from flask_cors import CORS

DATABASE_URL = os.environ["DATABASE_URL"]  # Supabase connection string
PRICE_CACHE_SECONDS = 15 * 60  # refresh prices at most every 15 min

app = Flask(__name__)
CORS(app)  # allow the frontend (different domain) to call this API


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


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT,
            type TEXT NOT NULL CHECK(type IN ('expense', 'income'))
        );

        CREATE TABLE IF NOT EXISTS holdings (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL CHECK(asset_type IN ('crypto', 'stock')),
            quantity REAL NOT NULL,
            avg_buy_price REAL NOT NULL,
            purchase_date TEXT
        );

        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT PRIMARY KEY,
            price REAL NOT NULL,
            last_updated BIGINT NOT NULL
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


# ---------- Transactions ----------

@app.route("/api/transactions", methods=["GET"])
def list_transactions():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC")
    return jsonify(cur.fetchall())


@app.route("/api/transactions", methods=["POST"])
def add_transaction():
    data = request.get_json(force=True)
    required = ["amount", "category", "type"]
    if not all(k in data and data[k] != "" for k in required):
        return jsonify({"error": "amount, category, and type are required"}), 400
    if data["type"] not in ("expense", "income"):
        return jsonify({"error": "type must be 'expense' or 'income'"}), 400

    date = data.get("date") or datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO transactions (date, amount, category, note, type) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (date, float(data["amount"]), data["category"], data.get("note", ""), data["type"]),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
def delete_transaction(tx_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
    db.commit()
    return "", 204


@app.route("/api/summary", methods=["GET"])
def summary():
    """Spend by category for the current month, plus totals."""
    db = get_db()
    cur = db.cursor()
    month_prefix = datetime.now().strftime("%Y-%m")
    cur.execute(
        """
        SELECT category, SUM(amount) as total
        FROM transactions
        WHERE type = 'expense' AND date LIKE %s
        GROUP BY category
        ORDER BY total DESC
        """,
        (f"{month_prefix}%",),
    )
    by_category = cur.fetchall()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN type = 'expense' AND date LIKE %s THEN amount END), 0) as spent,
            COALESCE(SUM(CASE WHEN type = 'income' AND date LIKE %s THEN amount END), 0) as earned
        """,
        (f"{month_prefix}%", f"{month_prefix}%"),
    )
    totals = cur.fetchone()

    return jsonify(
        {
            "by_category": by_category,
            "spent": totals["spent"],
            "earned": totals["earned"],
        }
    )


# ---------- Holdings + prices ----------

def fetch_price(symbol: str, asset_type: str):
    """Fetch live price, using cache when fresh enough."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM price_cache WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    now = int(time.time())
    if row and now - row["last_updated"] < PRICE_CACHE_SECONDS:
        return row["price"]

    price = None
    try:
        if asset_type == "crypto":
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": symbol.lower(), "vs_currencies": "usd"},
                timeout=10,
            )
            data = resp.json()
            price = data.get(symbol.lower(), {}).get("usd")
        else:
            api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "demo")
            resp = requests.get(
                "https://www.alphavantage.co/query",
                params={
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol.upper(),
                    "apikey": api_key,
                },
                timeout=10,
            )
            data = resp.json()
            price = float(data.get("Global Quote", {}).get("05. price", 0)) or None
    except (requests.RequestException, ValueError, KeyError):
        price = None

    if price is not None:
        cur.execute(
            """
            INSERT INTO price_cache (symbol, price, last_updated) VALUES (%s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET price = EXCLUDED.price, last_updated = EXCLUDED.last_updated
            """,
            (symbol, price, now),
        )
        db.commit()
        return price

    return row["price"] if row else None


@app.route("/api/holdings", methods=["GET"])
def list_holdings():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM holdings")
    rows = cur.fetchall()
    result = []
    for h in rows:
        h = dict(h)
        h["current_price"] = fetch_price(h["symbol"], h["asset_type"])
        h["current_value"] = (
            h["current_price"] * h["quantity"] if h["current_price"] else None
        )
        h["cost_basis"] = h["avg_buy_price"] * h["quantity"]
        h["gain_loss"] = (
            h["current_value"] - h["cost_basis"] if h["current_value"] else None
        )
        result.append(h)
    return jsonify(result)


@app.route("/api/holdings", methods=["POST"])
def add_holding():
    data = request.get_json(force=True)
    required = ["symbol", "asset_type", "quantity", "avg_buy_price"]
    if not all(k in data and data[k] != "" for k in required):
        return jsonify({"error": "symbol, asset_type, quantity, avg_buy_price are required"}), 400
    if data["asset_type"] not in ("crypto", "stock"):
        return jsonify({"error": "asset_type must be 'crypto' or 'stock'"}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO holdings (symbol, asset_type, quantity, avg_buy_price, purchase_date) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            data["symbol"],
            data["asset_type"],
            float(data["quantity"]),
            float(data["avg_buy_price"]),
            data.get("purchase_date"),
        ),
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"id": new_id}), 201


@app.route("/api/holdings/<int:holding_id>", methods=["DELETE"])
def delete_holding(holding_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM holdings WHERE id = %s", (holding_id,))
    db.commit()
    return "", 204


init_db()  # runs on import too, so gunicorn (used in production) creates the tables

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
