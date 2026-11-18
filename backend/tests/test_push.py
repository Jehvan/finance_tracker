"""Web push: digest text, subscription validation (SSRF guard), routes, daily run."""
import base64
import os
from datetime import date, timedelta

import pytest

import push
import recurring as rc
from conftest import CRON_SECRET

TODAY = date(2026, 11, 18)


def bill(name, days, reminder=3, active=True):
    return dict(name=name, next_due_date=TODAY + timedelta(days=days), reminder_days_before=reminder, is_active=active)


# ---------- digest (pure) ----------


def test_digest_none_when_nothing_due():
    assert rc.build_digest([bill("Rent", 10), bill("Gym", -5, active=False)], TODAY) is None


def test_digest_orders_most_urgent_first_and_has_no_amounts():
    d = rc.build_digest([bill("Netflix", 2), bill("Electricity", -2), bill("Phone", 0), bill("Rent", 9)], TODAY)
    assert d["title"] == "3 bills need attention"
    assert d["body"].split("\n") == ["Electricity: 2 days overdue", "Phone: due today", "Netflix: due in 2 days"]
    assert d["url"] == "/?tab=bills" and d["tag"] == "bills-digest"


def test_digest_singular_tomorrow_and_overflow():
    assert rc.build_digest([bill("Water", 1)], TODAY)["title"] == "1 bill needs attention"
    assert rc.build_digest([bill("Water", 1)], TODAY)["body"] == "Water: due tomorrow"
    d = rc.build_digest([bill(f"B{i}", -1) for i in range(5)], TODAY)
    assert d["body"].splitlines()[-1] == "+2 more"
    assert d["body"].splitlines()[0] == "B0: 1 day overdue"


def test_digest_truncates_long_names():
    d = rc.build_digest([bill("x" * 80, 0)], TODAY)
    assert d["body"] == "x" * 39 + "…: due today"


# ---------- subscription validation ----------


def b64(n):
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def sub(endpoint="https://fcm.googleapis.com/fcm/send/abc123", p256dh=None, auth=None):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh or b64(65), "auth": auth or b64(16)}}


@pytest.mark.parametrize("endpoint", [
    "https://fcm.googleapis.com/fcm/send/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x",
    "https://wns2-par02p.notify.windows.com/w/?token=x",
    "https://web.push.apple.com/QGx",
])
def test_known_push_services_accepted(endpoint):
    assert push.clean_subscription(sub(endpoint))["endpoint"] == endpoint


@pytest.mark.parametrize("endpoint", [
    "http://fcm.googleapis.com/fcm/send/x",              # not https
    "https://evil.example.com/x",                          # unknown host
    "https://fcm.googleapis.com.evil.com/x",               # suffix trick
    "https://evilfcm.googleapis.com.attacker.net/x",
    "https://user:pw@fcm.googleapis.com/x",                # credentials
    "https://fcm.googleapis.com:8443/x",                   # odd port
    "https://169.254.169.254/latest/meta-data",            # cloud metadata
    "https://localhost/x",
    "https://fcm.googleapis.com/" + "a" * 1000,           # too long
])
def test_other_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        push.clean_subscription(sub(endpoint))


@pytest.mark.parametrize("bad", [
    {"p256dh": "not base64!"},
    {"p256dh": base64.urlsafe_b64encode(os.urandom(10)).decode()},
    {"auth": base64.urlsafe_b64encode(os.urandom(40)).decode()},
])
def test_bad_keys_rejected(bad):
    s = sub()
    s["keys"].update(bad)
    with pytest.raises(ValueError):
        push.clean_subscription(s)


def test_non_object_rejected():
    for bad in (None, [], "x", {"endpoint": "https://fcm.googleapis.com/x"}):
        with pytest.raises(ValueError):
            push.clean_subscription(bad)


# ---------- routes ----------


@pytest.fixture
def sent(app_module, monkeypatch):
    """Capture notifications instead of sending them; results can be scripted per endpoint."""
    calls, results = [], {}

    def fake_send(subscription, payload, vapid, subject):
        calls.append((subscription["endpoint"], payload))
        return results.get(subscription["endpoint"], "sent")

    monkeypatch.setattr(push, "send_notification", fake_send)
    fake_send.calls, fake_send.results = calls, results
    return fake_send


def subscribe(client, headers, endpoint="https://fcm.googleapis.com/fcm/send/a"):
    return client.post("/api/push/subscription", json=sub(endpoint), headers=headers)


def subscription_rows(db_cursor):
    db_cursor.execute("SELECT user_id, endpoint, last_notified_on FROM push_subscriptions ORDER BY id")
    return db_cursor.fetchall()


def add_bill(client, headers, name, days, app_module):
    due = (app_module.app_today() + timedelta(days=days)).isoformat()
    res = client.post("/api/recurring", headers=headers,
                      json={"name": name, "amount": 100, "billing_cycle": "monthly", "next_due_date": due})
    assert res.status_code == 201


def run_daily(client, secret=CRON_SECRET):
    return client.post("/api/push/run-daily", headers={"X-Cron-Secret": secret} if secret is not None else {})


def test_config_exposes_public_key_only(client, alice):
    body = client.get("/api/push/config", headers=alice).get_json()
    assert body["enabled"] is True and len(body["public_key"]) == 87
    assert set(body) == {"enabled", "public_key"}


def test_subscribe_validates_and_upserts(client, alice, db_cursor):
    assert subscribe(client, alice).status_code == 201
    assert subscribe(client, alice).status_code == 201  # same browser again: no duplicate
    assert len(subscription_rows(db_cursor)) == 1
    assert subscribe(client, alice, "https://evil.example.com/x").status_code == 400
    assert client.post("/api/push/subscription", json=[1], headers=alice).status_code == 400


def test_subscriptions_capped_per_user(client, alice, db_cursor, app_module):
    for i in range(app_module.MAX_PUSH_SUBSCRIPTIONS_PER_USER + 3):
        subscribe(client, alice, f"https://fcm.googleapis.com/fcm/send/{i}")
    rows = subscription_rows(db_cursor)
    assert len(rows) == app_module.MAX_PUSH_SUBSCRIPTIONS_PER_USER
    assert rows[-1][1].endswith(f"/{app_module.MAX_PUSH_SUBSCRIPTIONS_PER_USER + 2}")  # newest kept


def test_unsubscribe_only_own(client, alice, bob, db_cursor):
    subscribe(client, alice)
    ep = "https://fcm.googleapis.com/fcm/send/a"
    assert client.delete("/api/push/subscription", json={"endpoint": ep}, headers=bob).status_code == 204
    assert len(subscription_rows(db_cursor)) == 1  # Bob couldn't remove Alice's device
    assert client.delete("/api/push/subscription", json={"endpoint": ep}, headers=alice).status_code == 204
    assert subscription_rows(db_cursor) == []


def test_push_routes_require_auth(client):
    assert client.get("/api/push/config").status_code == 401
    assert client.post("/api/push/subscription", json=sub()).status_code == 401
    assert client.post("/api/push/test").status_code == 401


def test_test_push_goes_only_to_own_devices_and_drops_dead_ones(client, alice, bob, sent, db_cursor):
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/alive")
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/dead")
    subscribe(client, bob, "https://fcm.googleapis.com/fcm/send/bob")
    sent.results["https://fcm.googleapis.com/fcm/send/dead"] = "gone"
    body = client.post("/api/push/test", headers=alice).get_json()
    assert body == {"sent": 1, "removed": 1, "failed": 0}
    assert sorted(ep for ep, _ in sent.calls) == ["https://fcm.googleapis.com/fcm/send/alive",
                                                  "https://fcm.googleapis.com/fcm/send/dead"]
    assert [r[1] for r in subscription_rows(db_cursor)] == ["https://fcm.googleapis.com/fcm/send/alive",
                                                           "https://fcm.googleapis.com/fcm/send/bob"]


# ---------- daily run ----------


@pytest.mark.parametrize("secret", [None, "", "wrong"])
def test_daily_run_needs_secret(client, sent, secret):
    assert run_daily(client, secret).status_code == 404
    assert sent.calls == []


def test_daily_run_disabled_without_configured_secret(client, sent, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "PUSH_CRON_SECRET", "")
    assert run_daily(client, "").status_code == 404


def test_daily_run_sends_each_user_their_own_digest_once(client, alice, bob, sent, app_module, db_cursor):
    add_bill(client, alice, "Electricity", -1, app_module)
    add_bill(client, alice, "Rent", 20, app_module)
    add_bill(client, bob, "Bob's phone", 1, app_module)
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/alice")
    subscribe(client, bob, "https://fcm.googleapis.com/fcm/send/bob")

    body = run_daily(client).get_json()
    assert body["sent"] == 2 and body["users"] == 2
    by_ep = dict(sent.calls)
    assert by_ep["https://fcm.googleapis.com/fcm/send/alice"]["body"] == "Electricity: 1 day overdue"
    assert by_ep["https://fcm.googleapis.com/fcm/send/bob"]["body"] == "Bob's phone: due tomorrow"
    assert "100" not in str(sent.calls)  # never amounts

    again = run_daily(client).get_json()  # retry / double trigger
    assert again["sent"] == 0 and len(sent.calls) == 2


def test_daily_run_skips_users_with_nothing_due(client, alice, sent, app_module):
    add_bill(client, alice, "Rent", 20, app_module)
    subscribe(client, alice)
    body = run_daily(client).get_json()
    assert body["nothing_due"] == 1 and sent.calls == []


def test_daily_run_retries_failures_and_removes_revoked(client, alice, sent, app_module, db_cursor):
    add_bill(client, alice, "Water", 0, app_module)
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/flaky")
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/revoked")
    sent.results.update({"https://fcm.googleapis.com/fcm/send/flaky": "failed",
                         "https://fcm.googleapis.com/fcm/send/revoked": "gone"})
    first = run_daily(client).get_json()
    assert first["failed"] == 1 and first["removed"] == 1
    rows = subscription_rows(db_cursor)
    assert [(r[1], r[2]) for r in rows] == [("https://fcm.googleapis.com/fcm/send/flaky", None)]  # claim released

    sent.results["https://fcm.googleapis.com/fcm/send/flaky"] = "sent"
    second = run_daily(client).get_json()
    assert second["sent"] == 1
    assert subscription_rows(db_cursor)[0][2] == app_module.app_today()


def test_send_does_not_follow_redirects(monkeypatch):
    """A redirect from a push service counts as a failure; the server never follows it."""
    import requests

    seen = {}

    def fake_post(self, url, **kwargs):
        seen["max_redirects"] = self.max_redirects
        raise requests.TooManyRedirects("redirects disabled")

    monkeypatch.setattr(requests.Session, "post", fake_post)
    vapid = push.load_vapid_key(push.generate_private_key_b64())
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    client_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    subscription = {"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                    "p256dh": base64.urlsafe_b64encode(client_key).rstrip(b"=").decode(), "auth": b64(16)}
    assert push.send_notification(subscription, {"title": "t"}, vapid, "https://example.com") == "failed"
    assert seen["max_redirects"] == 0
