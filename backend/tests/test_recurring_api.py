"""API tests against a real Postgres (see conftest.py): CRUD, ownership/IDOR,
mark-paid replay protection, upcoming/summary, and existing routes still working."""
from datetime import date, timedelta

import pytest

from conftest import auth_headers


def create(client, headers, **overrides):
    body = {"name": "Netflix", "amount": 9.99, "billing_cycle": "monthly", "next_due_date": "2027-01-31"}
    body.update(overrides)
    res = client.post("/api/recurring", json=body, headers=headers)
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def count_transactions(db_cursor):
    db_cursor.execute("SELECT count(*) FROM transactions")
    return db_cursor.fetchone()[0]


# ---------- auth ----------


def test_requires_auth(client):
    assert client.get("/api/recurring").status_code == 401
    assert client.post("/api/recurring/1/mark-paid", json={"due_date": "2027-01-01"}).status_code == 401


def test_token_without_sub_is_rejected(client):
    assert client.get("/api/recurring", headers=auth_headers(None)).status_code == 401


# ---------- CRUD ----------


def test_create_list_get_update_delete(client, alice):
    item = create(client, alice, currency="EUR", payment_method_label="Visa", notes="family plan")
    assert item["billing_anchor_day"] == 31
    assert item["currency"] == "EUR"
    assert "user_id" not in item
    assert item["monthly_cost"] == 9.99

    assert [i["id"] for i in client.get("/api/recurring", headers=alice).get_json()] == [item["id"]]
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).get_json()["name"] == "Netflix"

    res = client.patch(f"/api/recurring/{item['id']}", json={"is_active": False, "amount": "12.50"}, headers=alice)
    assert res.status_code == 200
    updated = res.get_json()
    assert updated["is_active"] is False and updated["amount"] == 12.5 and updated["status"] == "paused"
    assert updated["name"] == "Netflix"  # untouched fields kept

    assert client.delete(f"/api/recurring/{item['id']}", headers=alice).status_code == 204
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).status_code == 404
    assert client.delete(f"/api/recurring/{item['id']}", headers=alice).status_code == 404


def test_body_cannot_set_owner(client, alice, bob):
    item = create(client, alice, user_id="bob")
    assert client.get(f"/api/recurring/{item['id']}", headers=bob).status_code == 404
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "amount": -1, "billing_cycle": "monthly", "next_due_date": "2027-01-01"},
        {"name": "x", "amount": 5, "billing_cycle": "daily", "next_due_date": "2027-01-01"},
        {"name": "x", "amount": 5, "billing_cycle": "monthly", "next_due_date": "2027-02-30"},
        {"name": "x", "amount": 5, "billing_cycle": "monthly", "next_due_date": "2027-01-01",
         "payment_method_label": "4111111111111111"},
        {"amount": 5, "billing_cycle": "monthly", "next_due_date": "2027-01-01"},
    ],
)
def test_create_validation(client, alice, body):
    res = client.post("/api/recurring", json=body, headers=alice)
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_non_object_bodies_are_400_not_500(client, alice):
    assert client.post("/api/recurring", json=[1, 2], headers=alice).status_code == 400
    assert client.post("/api/recurring", data="not json", headers=alice).status_code == 400
    item = create(client, alice)
    assert client.patch(f"/api/recurring/{item['id']}", json="x", headers=alice).status_code == 400
    assert client.post(f"/api/recurring/{item['id']}/mark-paid", json=None, headers=alice).status_code == 400


def test_update_validation(client, alice):
    item = create(client, alice)
    assert client.patch(f"/api/recurring/{item['id']}", json={"amount": 0}, headers=alice).status_code == 400
    assert client.patch(f"/api/recurring/{item['id']}", json={}, headers=alice).status_code == 400


def test_changing_due_date_moves_anchor(client, alice):
    item = create(client, alice)
    res = client.patch(f"/api/recurring/{item['id']}", json={"next_due_date": "2027-03-15"}, headers=alice)
    assert res.get_json()["billing_anchor_day"] == 15


# ---------- ownership (IDOR) ----------


def test_user_cannot_touch_another_users_items(client, alice, bob, db_cursor):
    bobs = create(client, bob, name="Bob's rent", amount=500, category="rent", next_due_date="2027-01-31")
    url = f"/api/recurring/{bobs['id']}"

    assert client.get(url, headers=alice).status_code == 404
    assert client.put(url, json={"amount": 1}, headers=alice).status_code == 404
    assert client.patch(url, json={"is_active": False}, headers=alice).status_code == 404
    assert client.delete(url, headers=alice).status_code == 404
    assert client.post(f"{url}/mark-paid", json={"due_date": "2027-01-31"}, headers=alice).status_code == 404

    # Nothing leaked into Alice's views...
    assert client.get("/api/recurring", headers=alice).get_json() == []
    assert client.get("/api/recurring/upcoming?days=365", headers=alice).get_json()["count"] == 0
    assert client.get("/api/recurring/summary", headers=alice).get_json()["active_count"] == 0
    # ...and Bob's item is completely unchanged, with no expense created.
    assert client.get(url, headers=bob).get_json() == bobs
    assert count_transactions(db_cursor) == 0


def test_missing_and_foreign_ids_look_identical(client, alice, bob):
    bobs = create(client, bob)
    foreign = client.get(f"/api/recurring/{bobs['id']}", headers=alice)
    missing = client.get("/api/recurring/999999", headers=alice)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json() == missing.get_json()


# ---------- mark-paid ----------


def test_mark_paid_creates_linked_expense_and_advances(client, alice, db_cursor, app_module):
    item = create(client, alice, name="Phone", amount=600, category="phone", next_due_date="2027-01-31")
    res = client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-01-31"}, headers=alice)
    assert res.status_code == 201
    body = res.get_json()
    assert body["item"]["next_due_date"] == "2027-02-28"

    db_cursor.execute("SELECT * FROM transactions WHERE id = %s", (body["transaction_id"],))
    tx = db_cursor.fetchone()
    # columns: id, date, amount, category, note, type, recurring_payment_id, paid_for_due_date
    assert tx[1] == app_module.app_today().isoformat()
    assert float(tx[2]) == 600.0
    assert tx[3] == "Phone" and tx[4] == "Phone" and tx[5] == "expense"
    assert tx[6] == item["id"] and tx[7] == date(2027, 1, 31)

    # The following cycle snaps back to the 31st.
    res = client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-02-28"}, headers=alice)
    assert res.get_json()["item"]["next_due_date"] == "2027-03-31"


def test_mark_paid_converts_eur_to_mkd(client, alice, db_cursor):
    item = create(client, alice, amount=10, currency="EUR", next_due_date="2027-01-10")
    client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-01-10"}, headers=alice)
    db_cursor.execute("SELECT amount, category, note FROM transactions")
    amount, category, note = db_cursor.fetchone()
    assert float(amount) == 615.0
    assert category == "Subscriptions"
    assert note == "Netflix (€10.00)"


def test_mark_paid_cannot_be_replayed(client, alice, db_cursor):
    item = create(client, alice, next_due_date="2027-01-31")
    url = f"/api/recurring/{item['id']}/mark-paid"
    first = client.post(url, json={"due_date": "2027-01-31"}, headers=alice)
    replay = client.post(url, json={"due_date": "2027-01-31"}, headers=alice)
    assert first.status_code == 201
    assert replay.status_code == 409
    assert replay.get_json()["item"]["next_due_date"] == "2027-02-28"  # not advanced twice
    assert count_transactions(db_cursor) == 1


def test_mark_paid_db_unique_index_is_a_second_guard(client, alice, db_cursor):
    """Even if a matching expense somehow exists already, the DB index refuses a duplicate."""
    item = create(client, alice, next_due_date="2027-01-31")
    db_cursor.execute(
        "INSERT INTO transactions (date, amount, category, type, recurring_payment_id, paid_for_due_date) "
        "VALUES ('2027-01-31', 1, 'x', 'expense', %s, '2027-01-31')",
        (item["id"],),
    )
    res = client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-01-31"}, headers=alice)
    assert res.status_code == 409
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).get_json()["next_due_date"] == "2027-01-31"
    assert count_transactions(db_cursor) == 1


def test_mark_paid_requires_matching_valid_due_date(client, alice, db_cursor):
    item = create(client, alice, next_due_date="2027-01-31")
    url = f"/api/recurring/{item['id']}/mark-paid"
    assert client.post(url, json={}, headers=alice).status_code == 400
    assert client.post(url, json={"due_date": "nope"}, headers=alice).status_code == 400
    assert client.post(url, json={"due_date": "2027-02-28"}, headers=alice).status_code == 409
    assert count_transactions(db_cursor) == 0


def test_mark_paid_refused_when_paused(client, alice, db_cursor):
    item = create(client, alice, is_active=False)
    res = client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-01-31"}, headers=alice)
    assert res.status_code == 409
    assert count_transactions(db_cursor) == 0


def test_deleting_item_keeps_its_expenses(client, alice, db_cursor):
    item = create(client, alice, next_due_date="2027-01-31")
    client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": "2027-01-31"}, headers=alice)
    client.delete(f"/api/recurring/{item['id']}", headers=alice)
    db_cursor.execute("SELECT recurring_payment_id FROM transactions")
    assert db_cursor.fetchall() == [(None,)]


# ---------- upcoming + summary ----------


def test_upcoming_window_and_statuses(client, alice, app_module):
    today = app_module.app_today()
    iso = lambda n: (today + timedelta(days=n)).isoformat()
    create(client, alice, name="Overdue", next_due_date=iso(-2))
    create(client, alice, name="Soon", next_due_date=iso(2), reminder_days_before=3)
    create(client, alice, name="Week", next_due_date=iso(6), reminder_days_before=3)
    create(client, alice, name="Later", next_due_date=iso(20))
    create(client, alice, name="Paused", next_due_date=iso(1), is_active=False)

    body = client.get("/api/recurring/upcoming?days=7", headers=alice).get_json()
    assert [i["name"] for i in body["items"]] == ["Overdue", "Soon", "Week"]
    assert [i["status"] for i in body["items"]] == ["overdue", "due_soon", "ok"]
    assert [i["days_until_due"] for i in body["items"]] == [-2, 2, 6]
    assert body["count"] == 3 and body["overdue_count"] == 1

    assert client.get("/api/recurring/upcoming", headers=alice).get_json()["days"] == 7
    for bad in ("-1", "abc", "366", "1.5"):
        assert client.get(f"/api/recurring/upcoming?days={bad}", headers=alice).status_code == 400


def test_summary(client, alice):
    create(client, alice, name="Rent", amount=12000, category="rent")
    create(client, alice, name="Gym", amount=300, billing_cycle="weekly")
    create(client, alice, name="Domain", amount=12, billing_cycle="yearly", currency="EUR", category="other")
    create(client, alice, name="Paused", amount=99999, is_active=False)
    s = client.get("/api/recurring/summary", headers=alice).get_json()
    expected_monthly = 12000 + 300 * 52 / 12 + 1 * 61.5
    assert s["monthly_total"] == pytest.approx(expected_monthly, abs=0.01)
    assert s["yearly_total"] == pytest.approx(12000 * 12 + 300 * 52 + 12 * 61.5, abs=0.01)
    assert s["most_expensive"]["name"] == "Rent"
    assert s["active_count"] == 3


# ---------- existing features still work ----------


def test_existing_spending_routes_unaffected(client, alice):
    res = client.post(
        "/api/transactions",
        json={"amount": 250, "category": "Food", "note": "", "type": "expense", "date": "2026-11-18"},
        headers=alice,
    )
    assert res.status_code == 201
    assert len(client.get("/api/transactions?month=2026-11", headers=alice).get_json()) == 1
    summary = client.get("/api/summary?month=2026-11", headers=alice).get_json()
    assert summary["spent"] == 250
