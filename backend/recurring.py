"""Subscriptions & Bills: pure logic with no Flask or database imports.

Kept separate from app.py so the date math, cost normalization and input
validation can be unit-tested without a database.
"""
import calendar
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CYCLES = ("weekly", "monthly", "quarterly", "yearly")
CATEGORIES = ("subscription", "utility", "rent", "phone", "other")
CURRENCIES = ("MKD", "EUR")

# Same fixed peg the frontend uses for the Spending form (the denar has been
# pegged to the euro at ~61.5 since 1997), so totals agree everywhere.
BASE_CURRENCY = "MKD"
EUR_TO_MKD_RATE = Decimal("61.5")

# Spending-table category each recurring category is logged under on "mark paid".
EXPENSE_CATEGORY = {
    "subscription": "Subscriptions",
    "utility": "Utilities",
    "rent": "Rent",
    "phone": "Phone",
    "other": "Bills",
}

MAX_AMOUNT = Decimal("9999999999.99")  # NUMERIC(12,2) upper bound
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_REMINDER_DAYS = 60

_MONTHS_PER_CYCLE = {"monthly": 1, "quarterly": 3, "yearly": 12}
# How many times a year each cycle bills. Monthly cost = yearly cost / 12,
# so weekly is normalized as 52 weeks / 12 months (not 4 weeks a month).
_PER_YEAR = {"weekly": 52, "monthly": 12, "quarterly": 4, "yearly": 1}
CENT = Decimal("0.01")


# ---------- Dates ----------

def add_months(d, months, anchor_day):
    """Move `d` forward `months` months, landing on `anchor_day` or, in a
    shorter month, that month's last day (Jan 31 -> Feb 28/29)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(anchor_day, last_day))


def advance_due_date(due, cycle, anchor_day=None):
    """The due date one billing cycle after `due`.

    `anchor_day` is the day-of-month the bill really falls on. Passing it keeps
    a bill due on the 31st from drifting: Jan 31 -> Feb 28 -> Mar 31, not Mar 28.
    """
    if cycle == "weekly":
        return due + timedelta(days=7)
    if cycle not in _MONTHS_PER_CYCLE:
        raise ValueError(f"unknown billing cycle: {cycle!r}")
    return add_months(due, _MONTHS_PER_CYCLE[cycle], anchor_day or due.day)


def due_status(next_due, today, reminder_days_before, is_active=True):
    """'paused' | 'overdue' | 'due_soon' (inside the reminder window) | 'ok'."""
    if not is_active:
        return "paused"
    days = (next_due - today).days
    if days < 0:
        return "overdue"
    if days <= reminder_days_before:
        return "due_soon"
    return "ok"


# ---------- Money ----------

def to_decimal(value):
    # psycopg2 hands NUMERIC back as float in this app (see DEC2FLOAT in app.py);
    # going through str() recovers the exact 2-decimal value.
    return value if isinstance(value, Decimal) else Decimal(str(value))


def quantize(value):
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def yearly_cost(amount, cycle):
    return to_decimal(amount) * _PER_YEAR[cycle]


def monthly_cost(amount, cycle):
    return yearly_cost(amount, cycle) / 12


def to_base_currency(amount, currency):
    amount = to_decimal(amount)
    return amount * EUR_TO_MKD_RATE if currency == "EUR" else amount


def summarize(items):
    """Totals across active items. Combined totals are in MKD (EUR converted
    at the fixed peg); per-currency totals are in their own currency."""
    yearly_base = Decimal(0)
    by_currency, by_category = {}, {}
    most_expensive, most_expensive_monthly = None, None

    for item in items:
        if not item["is_active"]:
            continue
        cur, cycle = item["currency"], item["billing_cycle"]
        yearly = yearly_cost(item["amount"], cycle)
        yearly_in_base = to_base_currency(yearly, cur)
        yearly_base += yearly_in_base

        c = by_currency.setdefault(cur, {"currency": cur, "yearly": Decimal(0), "count": 0})
        c["yearly"] += yearly
        c["count"] += 1

        k = by_category.setdefault(item["category"], {"category": item["category"], "yearly": Decimal(0), "count": 0})
        k["yearly"] += yearly_in_base
        k["count"] += 1

        monthly_in_base = yearly_in_base / 12
        if most_expensive_monthly is None or monthly_in_base > most_expensive_monthly:
            most_expensive_monthly = monthly_in_base
            most_expensive = {
                "id": item["id"],
                "name": item["name"],
                "amount": float(quantize(item["amount"])),
                "currency": cur,
                "billing_cycle": cycle,
                "monthly_in_base": float(quantize(monthly_in_base)),
            }

    def finish(group):
        return {**group, "monthly": float(quantize(group["yearly"] / 12)), "yearly": float(quantize(group["yearly"]))}

    return {
        "base_currency": BASE_CURRENCY,
        "eur_to_mkd_rate": float(EUR_TO_MKD_RATE),
        "monthly_total": float(quantize(yearly_base / 12)),
        "yearly_total": float(quantize(yearly_base)),
        "active_count": sum(c["count"] for c in by_currency.values()),
        "by_currency": sorted((finish(c) for c in by_currency.values()), key=lambda c: c["currency"]),
        "by_category": sorted((finish(k) for k in by_category.values()), key=lambda k: -k["yearly"]),
        "most_expensive": most_expensive,
    }


# ---------- Input validation ----------

def parse_date(value, field):
    """Strict YYYY-MM-DD within a sane range, or ValueError."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} is not a real calendar date")
    if not MIN_DATE <= d <= MAX_DATE:
        raise ValueError(f"{field} must be between {MIN_DATE} and {MAX_DATE}")
    return d


def _amount(v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError("amount must be a number")
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        raise ValueError("amount must be a number")
    if not d.is_finite():
        raise ValueError("amount must be a finite number")
    d = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if d <= 0:
        raise ValueError("amount must be greater than 0")
    if d > MAX_AMOUNT:
        raise ValueError("amount is too large")
    return d


def _text(v, field, max_len, required):
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(v, str):
        raise ValueError(f"{field} must be text")
    v = v.strip()
    if len(v) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return v


def _choice(v, field, allowed, upper=False):
    if isinstance(v, str):
        v = v.strip().upper() if upper else v.strip().lower()
    if v not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return v


def _payment_method(v):
    v = _text(v, "payment_method_label", 40, required=False)
    if v is not None and sum(ch.isdigit() for ch in v) > 4:
        # Labels like "Visa" or "Visa ••1234" only; never store a card number.
        raise ValueError("payment_method_label must not contain a card number (at most the last 4 digits)")
    return v


def _reminder_days(v):
    if isinstance(v, bool):
        raise ValueError("reminder_days_before must be a whole number")
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int):
        raise ValueError("reminder_days_before must be a whole number")
    if not 0 <= v <= MAX_REMINDER_DAYS:
        raise ValueError(f"reminder_days_before must be between 0 and {MAX_REMINDER_DAYS}")
    return v


def _bool(v, field):
    if not isinstance(v, bool):
        raise ValueError(f"{field} must be true or false")
    return v


_VALIDATORS = {
    "name": lambda v: _text(v, "name", 100, required=True),
    "amount": _amount,
    "currency": lambda v: _choice(v, "currency", CURRENCIES, upper=True),
    "billing_cycle": lambda v: _choice(v, "billing_cycle", CYCLES),
    "next_due_date": lambda v: parse_date(v, "next_due_date"),
    "category": lambda v: _choice(v, "category", CATEGORIES),
    "payment_method_label": _payment_method,
    "reminder_days_before": _reminder_days,
    "is_active": lambda v: _bool(v, "is_active"),
    "notes": lambda v: _text(v, "notes", 500, required=False),
}
REQUIRED_ON_CREATE = ("name", "amount", "billing_cycle", "next_due_date")


def clean_payload(data, partial=False):
    """Validate a create (partial=False) or update (partial=True) body.

    Returns a dict containing only known, validated columns; unknown keys
    (including user_id or id) are ignored, so they can never be set by the client.
    Raises ValueError with a user-facing message.
    """
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    if not partial:
        missing = [f for f in REQUIRED_ON_CREATE if data.get(f) in (None, "")]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")

    cleaned = {field: validate(data[field]) for field, validate in _VALIDATORS.items() if field in data}

    if partial and not cleaned:
        raise ValueError("no updatable fields provided")
    if "next_due_date" in cleaned:
        cleaned["billing_anchor_day"] = cleaned["next_due_date"].day
    return cleaned


# ---------- Push notification digest ----------

DIGEST_MAX_LINES = 3
DIGEST_NAME_MAX = 40


def _due_phrase(days):
    if days < -1:
        return f"{-days} days overdue"
    if days == -1:
        return "1 day overdue"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    return f"due in {days} days"


def build_digest(items, today):
    """The daily reminder notification for one user, or None if nothing needs attention.

    `items` are bill rows (name, next_due_date, reminder_days_before, is_active).
    Lock screens are visible to anyone nearby, so the text names bills and
    when they're due but never includes amounts.
    """
    attention = []
    for item in items:
        status = due_status(item["next_due_date"], today, item["reminder_days_before"], item["is_active"])
        if status in ("overdue", "due_soon"):
            attention.append(((item["next_due_date"] - today).days, item["name"]))
    if not attention:
        return None

    attention.sort()
    n = len(attention)
    lines = []
    for days, name in attention[:DIGEST_MAX_LINES]:
        if len(name) > DIGEST_NAME_MAX:
            name = name[: DIGEST_NAME_MAX - 1] + "…"
        lines.append(f"{name}: {_due_phrase(days)}")
    if n > DIGEST_MAX_LINES:
        lines.append(f"+{n - DIGEST_MAX_LINES} more")
    return {
        "title": "1 bill needs attention" if n == 1 else f"{n} bills need attention",
        "body": "\n".join(lines),
        "url": "/?tab=bills",
        "tag": "bills-digest",  # a newer digest replaces the old one instead of stacking
    }
