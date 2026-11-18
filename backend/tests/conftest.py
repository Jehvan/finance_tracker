"""Test fixtures.

Pure-logic tests need nothing. API tests need a THROWAWAY Postgres database:

    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/finance_test pytest

Every API test TRUNCATES the recurring_payments and transactions tables, so
never point TEST_DATABASE_URL at Supabase/production. API tests are skipped
when TEST_DATABASE_URL isn't set.
"""
import os
from datetime import datetime, timedelta, timezone

import jwt
import psycopg2
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
TEST_SECRET = "test-only-signing-key-0123456789abcdef-not-a-real-secret"


@pytest.fixture(scope="session")
def app_module():
    if not TEST_DB:
        pytest.skip("set TEST_DATABASE_URL to a throwaway Postgres database to run API tests")
    if "supabase" in TEST_DB:
        pytest.exit("TEST_DATABASE_URL points at Supabase; tests truncate tables. Use a local database.")
    os.environ.update(
        DATABASE_URL=TEST_DB,
        SECRET_KEY=TEST_SECRET,
        ADMIN_PASSWORD_HASH="unused-in-tests",
        FRONTEND_ORIGIN="http://localhost:5173",
    )
    import app  # runs init_db(), which applies the migrations

    app.app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app_module):
    conn = psycopg2.connect(TEST_DB)
    with conn, conn.cursor() as cur:
        cur.execute("TRUNCATE transactions, recurring_payments RESTART IDENTITY CASCADE")
    conn.close()
    return app_module.app.test_client()


@pytest.fixture
def db_cursor():
    conn = psycopg2.connect(TEST_DB)
    conn.autocommit = True
    cur = conn.cursor()
    yield cur
    conn.close()


def auth_headers(sub):
    payload = {"exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    if sub is not None:
        payload["sub"] = sub
    return {"Authorization": f"Bearer {jwt.encode(payload, TEST_SECRET, algorithm='HS256')}"}


@pytest.fixture
def alice():
    return auth_headers("alice")


@pytest.fixture
def bob():
    return auth_headers("bob")
