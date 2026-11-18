"""Web push: digest text, subscription validation (SSRF guard), routes, daily run."""
import base64
import os
from datetime import date, timedelta

import pytest

import push
import recurring as rc
from conftest import CRON_SECRET

TODAY = date(2026, 11, 18)


_ids = iter(range(1, 10_000))


def bill(name, days, reminder=7, active=True, heads_up_for=None):
    due = TODAY + timedelta(days=days)
    return dict(id=next(_ids), name=name, next_due_date=due, reminder_days_before=reminder,
                is_active=active, heads_up_sent_for=heads_up_for)


# ---------- which bills notify, and the digest text (pure) ----------


@pytest.mark.parametrize("days, reminder, heads_up_done, expected", [
    (7, 7, False, True),    # heads-up a week before
    (4, 7, False, True),    # bill added late / skipped day: heads-up still comes
    (4, 7, True, False),    # ...but only once per due date
    (8, 7, False, False),   # not yet
    (1, 7, True, False),    # quiet between heads-up and due day
    (0, 7, True, True),     # due day, always
    (-3, 7, True, True),    # overdue, daily
    (2, 0, False, False),   # "on the day" setting: nothing before the due date
    (0, 0, False, True),
])
def test_needs_notification(days, reminder, heads_up_done, expected):
    b = bill("x", days, reminder)
    if heads_up_done:
        b["heads_up_sent_for"] = b["next_due_date"]
    assert rc.needs_notification(b, TODAY) is expected


def test_stale_heads_up_marker_from_previous_cycle_rearms():
    b = bill("x", 7, heads_up_for=TODAY - timedelta(days=23))  # last month's due date
    assert rc.needs_notification(b, TODAY) is True


def test_paused_never_notifies():
    assert rc.needs_notification(bill("x", -5, active=False), TODAY) is False


def test_digest_none_when_nothing_due():
    assert rc.build_digest([bill("Rent", 10), bill("Gym", -5, active=False)], TODAY) == (None, [])


def test_digest_orders_most_urgent_first_and_has_no_amounts():
    netflix, elec, phone, rent = bill("Netflix", 7), bill("Electricity", -2), bill("Phone", 0), bill("Rent", 9)
    d, heads_up = rc.build_digest([netflix, elec, phone, rent], TODAY)
    assert d["title"] == "3 bills need attention"
    assert d["body"].split("\n") == ["Electricity: 2 days overdue", "Phone: due today", "Netflix: due in 7 days"]
    assert d["url"] == "/?tab=bills" and d["tag"] == "bills-digest"
    assert heads_up == [netflix["id"]]  # only the advance notice is tracked


def test_digest_singular_tomorrow_and_overflow():
    d, _ = rc.build_digest([bill("Water", 1)], TODAY)
    assert d["title"] == "1 bill needs attention" and d["body"] == "Water: due tomorrow"
    d, _ = rc.build_digest([bill(f"B{i}", -1) for i in range(5)], TODAY)
    assert d["body"].splitlines()[-1] == "+2 more"
    assert d["body"].splitlines()[0] == "B0: 1 day overdue"


def test_digest_truncates_long_names():
    d, _ = rc.build_digest([bill("x" * 80, 0)], TODAY)
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


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_vapid_key_means_push_off(value):
    assert push.load_vapid_key(value) is None


@pytest.mark.parametrize("value", [
    "not base64!",
    b64(65),                 # the public key pasted by mistake
    b64(16),                 # too short
    base64.urlsafe_b64encode(b"\x00" * 32).rstrip(b"=").decode(),  # 0 is not a valid P-256 key
])
def test_malformed_vapid_key_is_rejected_cleanly(value):
    with pytest.raises(ValueError):
        push.load_vapid_key(value)


def test_generated_and_dotnet_style_keys_load():
    k = push.generate_private_key_b64()
    assert len(k) == 43 and push.load_vapid_key(k) is not None
    assert push.load_vapid_key("  " + k + "\n") is not None  # stray whitespace from copy-paste


def test_api_starts_with_a_bad_key(app_module):  # app_module: sets the env the import needs
    """A malformed VAPID_PRIVATE_KEY disables push instead of crashing the API at import."""
    import subprocess, sys
    env = {**os.environ, "VAPID_PRIVATE_KEY": b64(65)}
    code = "import app; print('ok', app.VAPID is None)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.strip().endswith("ok True"), out.stderr[-500:]
    assert "Push notifications disabled" in out.stderr



def test_heads_up_once_then_due_day_then_rearmed_after_paying(client, alice, sent, app_module, monkeypatch, db_cursor):
    today = app_module.app_today()
    add_bill(client, alice, "Internet", 7, app_module)  # default reminder is now 7 days
    subscribe(client, alice)

    def run_on(day_offset):
        monkeypatch.setattr(app_module, "app_today", lambda: today + timedelta(days=day_offset))
        before = len(sent.calls)
        run_daily(client)
        return [p["body"] for _, p in sent.calls[before:]]

    assert run_on(0) == ["Internet: due in 7 days"]   # heads-up a week before
    assert run_on(1) == []                             # no repeat the next days
    assert run_on(6) == []
    assert run_on(7) == ["Internet: due today"]        # due day
    assert run_on(8) == ["Internet: 1 day overdue"]    # then daily while overdue

    # Paying moves the due date a month on; that cycle gets its own heads-up.
    monkeypatch.setattr(app_module, "app_today", lambda: today + timedelta(days=8))
    item = client.get("/api/recurring", headers=alice).get_json()[0]
    assert client.post(f"/api/recurring/{item['id']}/mark-paid", json={"due_date": item["next_due_date"]},
                       headers=alice).status_code == 201
    new_due = date.fromisoformat(client.get("/api/recurring", headers=alice).get_json()[0]["next_due_date"])
    offset = (new_due - today).days - 7
    assert run_on(offset) == ["Internet: due in 7 days"]


def test_failed_send_keeps_heads_up_for_retry(client, alice, sent, app_module):
    add_bill(client, alice, "Water", 5, app_module)
    subscribe(client, alice, "https://fcm.googleapis.com/fcm/send/flaky")
    sent.results["https://fcm.googleapis.com/fcm/send/flaky"] = "failed"
    run_daily(client)
    sent.results["https://fcm.googleapis.com/fcm/send/flaky"] = "sent"
    run_daily(client)  # retry the same day
    assert [p["body"] for _, p in sent.calls] == ["Water: due in 5 days"] * 2


def test_new_bills_default_to_7_days(client, alice):
    res = client.post("/api/recurring", headers=alice,
                      json={"name": "x", "amount": 1, "billing_cycle": "monthly", "next_due_date": "2027-01-01"})
    assert res.get_json()["reminder_days_before"] == 7
    assert "heads_up_sent_for" not in res.get_json()  # internal tracking stays server-side


def test_existing_3_day_bills_move_to_7_only_once(client, alice, app_module, db_cursor):
    """Migration 005 bumps old-default bills once; later deliberate 3s are left alone on restart."""
    item = client.post("/api/recurring", headers=alice, json={
        "name": "x", "amount": 1, "billing_cycle": "monthly", "next_due_date": "2027-01-01",
        "reminder_days_before": 3}).get_json()
    db_cursor.execute("DELETE FROM app_migrations WHERE name = '005_existing_reminders_to_7'")
    app_module.init_db()  # first deploy with 005: bumps 3 -> 7
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).get_json()["reminder_days_before"] == 7
    client.patch(f"/api/recurring/{item['id']}", headers=alice, json={"reminder_days_before": 3})
    app_module.init_db()  # every later restart: no change
    assert client.get(f"/api/recurring/{item['id']}", headers=alice).get_json()["reminder_days_before"] == 3
