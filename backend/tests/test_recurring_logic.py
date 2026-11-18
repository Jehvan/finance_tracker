"""Pure-logic tests: date advancing, monthly normalization, validation. No DB needed."""
from datetime import date
from decimal import Decimal

import pytest

import recurring as rc

# ---------- advance_due_date ----------


@pytest.mark.parametrize(
    "due, anchor, expected",
    [
        (date(2027, 1, 31), 31, date(2027, 2, 28)),  # non-leap February
        (date(2028, 1, 31), 31, date(2028, 2, 29)),  # leap February
        (date(2027, 2, 28), 31, date(2027, 3, 31)),  # snaps back to the anchor day
        (date(2027, 3, 31), 31, date(2027, 4, 30)),  # 30-day month
        (date(2027, 1, 30), 30, date(2027, 2, 28)),
        (date(2027, 12, 15), 15, date(2028, 1, 15)),  # year rollover
        (date(2027, 6, 1), 1, date(2027, 7, 1)),
    ],
)
def test_monthly_advance_handles_month_ends(due, anchor, expected):
    assert rc.advance_due_date(due, "monthly", anchor) == expected


def test_monthly_chain_does_not_drift_after_short_month():
    d = date(2027, 1, 31)
    seen = []
    for _ in range(12):
        d = rc.advance_due_date(d, "monthly", 31)
        seen.append(d)
    assert seen[0] == date(2027, 2, 28)
    assert seen[1] == date(2027, 3, 31)
    assert seen[-1] == date(2028, 1, 31)
    assert all(x.day == 31 or x.month in (2, 4, 6, 9, 11) for x in seen)


def test_anchor_defaults_to_due_day():
    assert rc.advance_due_date(date(2027, 1, 31), "monthly") == date(2027, 2, 28)


@pytest.mark.parametrize(
    "due, anchor, expected",
    [
        (date(2026, 11, 30), 30, date(2027, 2, 28)),
        (date(2027, 11, 30), 30, date(2028, 2, 29)),
        (date(2027, 10, 31), 31, date(2028, 1, 31)),
        (date(2027, 2, 28), 31, date(2027, 5, 31)),
    ],
)
def test_quarterly_advance(due, anchor, expected):
    assert rc.advance_due_date(due, "quarterly", anchor) == expected


@pytest.mark.parametrize(
    "due, anchor, expected",
    [
        (date(2028, 2, 29), 29, date(2029, 2, 28)),  # leap day -> non-leap year
        (date(2031, 2, 28), 29, date(2032, 2, 29)),  # back to the leap day when it exists
        (date(2026, 12, 31), 31, date(2027, 12, 31)),
    ],
)
def test_yearly_advance(due, anchor, expected):
    assert rc.advance_due_date(due, "yearly", anchor) == expected


def test_weekly_advance_crosses_month_and_year():
    assert rc.advance_due_date(date(2026, 12, 28), "weekly", 28) == date(2027, 1, 4)
    assert rc.advance_due_date(date(2028, 2, 25), "weekly") == date(2028, 3, 3)


def test_unknown_cycle_raises():
    with pytest.raises(ValueError):
        rc.advance_due_date(date(2027, 1, 1), "daily")


# ---------- due_status ----------


@pytest.mark.parametrize(
    "due, reminder, active, expected",
    [
        (date(2026, 11, 17), 3, True, "overdue"),
        (date(2026, 11, 18), 3, True, "due_soon"),  # due today
        (date(2026, 11, 21), 3, True, "due_soon"),  # edge of window
        (date(2026, 11, 22), 3, True, "ok"),
        (date(2026, 11, 18), 0, True, "due_soon"),
        (date(2026, 11, 1), 3, False, "paused"),
    ],
)
def test_due_status(due, reminder, active, expected):
    assert rc.due_status(due, date(2026, 11, 18), reminder, active) == expected


# ---------- normalization ----------


@pytest.mark.parametrize(
    "amount, cycle, monthly, yearly",
    [
        (10, "monthly", "10.00", "120.00"),
        (12, "weekly", "52.00", "624.00"),  # 52 weeks / 12, not 4 weeks a month
        (300, "quarterly", "100.00", "1200.00"),
        (1200, "yearly", "100.00", "1200.00"),
        (9.99, "monthly", "9.99", "119.88"),
        (100, "weekly", "433.33", "5200.00"),
    ],
)
def test_monthly_and_yearly_normalization(amount, cycle, monthly, yearly):
    assert rc.quantize(rc.monthly_cost(amount, cycle)) == Decimal(monthly)
    assert rc.quantize(rc.yearly_cost(amount, cycle)) == Decimal(yearly)


def test_eur_converts_at_peg():
    assert rc.quantize(rc.to_base_currency(10, "EUR")) == Decimal("615.00")
    assert rc.quantize(rc.to_base_currency(10, "MKD")) == Decimal("10.00")


def _item(id, name, amount, cycle, currency="MKD", category="subscription", active=True):
    return dict(id=id, name=name, amount=amount, billing_cycle=cycle, currency=currency,
                category=category, is_active=active)


def test_summarize_mixed_currencies_and_paused():
    s = rc.summarize([
        _item(1, "Rent", 12000, "monthly", category="rent"),
        _item(2, "Netflix", 10, "monthly", currency="EUR"),            # 615 MKD/month
        _item(3, "Domain", 120, "yearly", currency="EUR", category="other"),  # 10 EUR/month = 615 MKD
        _item(4, "Gym", 99999, "monthly", active=False),               # paused: ignored
    ])
    assert s["monthly_total"] == 12000 + 615 + 615
    assert s["yearly_total"] == (12000 + 615 + 615) * 12
    assert s["active_count"] == 3
    assert s["most_expensive"]["name"] == "Rent"
    by_cur = {c["currency"]: c for c in s["by_currency"]}
    assert by_cur["EUR"]["monthly"] == 20 and by_cur["EUR"]["yearly"] == 240 and by_cur["EUR"]["count"] == 2
    assert by_cur["MKD"]["monthly"] == 12000
    assert [c["category"] for c in s["by_category"]] == ["rent", "subscription", "other"]


def test_summarize_empty():
    s = rc.summarize([])
    assert s["monthly_total"] == 0 and s["yearly_total"] == 0 and s["most_expensive"] is None


# ---------- validation ----------

VALID = {"name": "Netflix", "amount": "9.99", "billing_cycle": "monthly", "next_due_date": "2027-01-31"}


def test_clean_payload_valid_sets_anchor_and_ignores_unknown_fields():
    out = rc.clean_payload({**VALID, "user_id": "mallory", "id": 5, "currency": "eur"})
    assert out["amount"] == Decimal("9.99")
    assert out["next_due_date"] == date(2027, 1, 31)
    assert out["billing_anchor_day"] == 31
    assert out["currency"] == "EUR"
    assert "user_id" not in out and "id" not in out


@pytest.mark.parametrize(
    "override, message",
    [
        ({"amount": "-5"}, "greater than 0"),
        ({"amount": 0}, "greater than 0"),
        ({"amount": "0.001"}, "greater than 0"),
        ({"amount": "abc"}, "number"),
        ({"amount": "NaN"}, "finite"),
        ({"amount": "Infinity"}, "finite"),
        ({"amount": True}, "number"),
        ({"amount": "1e20"}, "too large"),
        ({"billing_cycle": "daily"}, "billing_cycle"),
        ({"currency": "USD"}, "currency"),
        ({"category": "food"}, "category"),
        ({"next_due_date": "2027-02-30"}, "real calendar date"),
        ({"next_due_date": "31/01/2027"}, "YYYY-MM-DD"),
        ({"next_due_date": "1999-12-31"}, "between"),
        ({"name": "   "}, "required"),
        ({"name": "x" * 101}, "at most 100"),
        ({"name": 123}, "text"),
        ({"payment_method_label": "4111 1111 1111 1111"}, "card number"),
        ({"reminder_days_before": -1}, "between 0 and 60"),
        ({"reminder_days_before": 61}, "between 0 and 60"),
        ({"reminder_days_before": 2.5}, "whole number"),
        ({"is_active": "yes"}, "true or false"),
        ({"notes": "n" * 501}, "at most 500"),
    ],
)
def test_clean_payload_rejects_bad_input(override, message):
    with pytest.raises(ValueError, match=message):
        rc.clean_payload({**VALID, **override})


def test_clean_payload_requires_fields_on_create_only():
    with pytest.raises(ValueError, match="missing required fields: amount"):
        rc.clean_payload({k: v for k, v in VALID.items() if k != "amount"})
    assert rc.clean_payload({"is_active": False}, partial=True) == {"is_active": False}
    with pytest.raises(ValueError, match="no updatable fields"):
        rc.clean_payload({"user_id": "x"}, partial=True)
    with pytest.raises(ValueError, match="JSON object"):
        rc.clean_payload(["not", "a", "dict"])


def test_payment_method_allows_last_four():
    assert rc.clean_payload({**VALID, "payment_method_label": "Visa ••1234"})["payment_method_label"] == "Visa ••1234"
    assert rc.clean_payload({**VALID, "payment_method_label": ""})["payment_method_label"] is None
