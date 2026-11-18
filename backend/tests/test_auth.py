"""Cookie sessions, CSRF header, and login rate limiting."""
from conftest import ADMIN_PASSWORD, ADMIN_USERNAME

CSRF = {"X-Requested-With": "fetch"}
HTTPS = "https://localhost"  # the session cookie is Secure, so use https in the test client


def login(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD, headers=CSRF):
    return client.post("/api/login", json={"username": username, "password": password},
                       headers=headers, base_url=HTTPS)


def test_login_sets_httponly_strict_cookie_and_returns_no_token(client):
    res = login(client)
    assert res.status_code == 200
    assert res.get_json() == {"username": ADMIN_USERNAME}  # token is NOT in the body
    cookie = res.headers["Set-Cookie"]
    assert cookie.startswith("ft_session=")
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/api"):
        assert attr in cookie


def test_cookie_session_works_and_logout_clears_it(client):
    login(client)
    assert client.get("/api/session", base_url=HTTPS).get_json() == {"username": ADMIN_USERNAME}
    assert client.get("/api/recurring", base_url=HTTPS).status_code == 200
    client.post("/api/logout", base_url=HTTPS)
    assert client.get("/api/session", base_url=HTTPS).status_code == 401


def test_cookie_writes_require_csrf_header(client):
    login(client)
    body = {"name": "Netflix", "amount": 5, "billing_cycle": "monthly", "next_due_date": "2027-01-01"}
    assert client.post("/api/recurring", json=body, base_url=HTTPS).status_code == 403
    assert client.post("/api/recurring", json=body, headers=CSRF, base_url=HTTPS).status_code == 201
    # reads don't need it
    assert client.get("/api/recurring", base_url=HTTPS).status_code == 200


def test_login_requires_csrf_header(client):
    assert login(client, headers={}).status_code == 403


def test_no_session_is_401(client):
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/transactions").status_code == 401


def test_bad_login_bodies(client):
    assert client.post("/api/login", json=["x"], headers=CSRF).status_code == 400
    assert client.post("/api/login", json={"username": 1, "password": 2}, headers=CSRF).status_code == 401


def test_wrong_username_and_wrong_password_look_identical(client):
    a = login(client, username="nobody")
    b = login(client, password="wrong")
    assert a.status_code == b.status_code == 401
    assert a.get_json() == b.get_json()


def test_rate_limit_blocks_after_five_failures_even_with_right_password(client):
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    blocked = login(client)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert "Set-Cookie" not in blocked.headers


def test_rate_limit_is_per_username_and_case_insensitive(client):
    for _ in range(5):
        login(client, username="Admin", password="wrong")
    assert login(client, username="ADMIN").status_code == 429
    assert login(client, username="someone-else", password="x").status_code == 401  # not blocked


def test_global_cap_blocks_username_spraying(client, app_module):
    for i in range(app_module.LOGIN_MAX_FAILURES_TOTAL):
        login(client, username=f"spray{i}", password="x")
    assert login(client).status_code == 429


def test_success_resets_username_failures(client):
    for _ in range(4):
        login(client, password="wrong")
    assert login(client).status_code == 200
    for _ in range(4):
        assert login(client, password="wrong").status_code == 401
    assert login(client).status_code == 200


def test_old_failures_expire(client, db_cursor):
    for _ in range(5):
        login(client, password="wrong")
    db_cursor.execute("UPDATE login_failures SET failed_at = now() - interval '16 minutes'")
    assert login(client).status_code == 200
