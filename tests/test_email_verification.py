"""Tests for direct account activation and login without email verification."""
import sqlite3
from pathlib import Path
import pytest

from auth.company_auth import (
    register_company_account,
    authenticate_user,
    request_password_reset,
)
from database.connection import init_db, get_db_connection


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create a clean isolated SQLite database for testing."""
    monkeypatch.setenv("SIGNUP_CODE", "")
    db_file = tmp_path / "test_auth.db"
    init_db(db_file)
    return db_file


def test_signup_logs_in_directly(temp_db):
    """Test that sign-up directly activates the account without any verification code step."""
    ok, msg, sess = register_company_account(
        company_name="Gulf Direct Trading LLC",
        trn="100555666777003",
        owner_name="Fatima Al Mansoori",
        email="fatima@gulfdirect.ae",
        password="SecurePassword123!",
        db_path=temp_db,
    )

    assert ok is True
    assert sess is not None
    assert sess.get("unverified") is not True
    assert sess.get("email") == "fatima@gulfdirect.ae"
    assert sess.get("role") == "OWNER"
    assert "user_id" in sess and sess["user_id"] is not None

    # Verify user is active in DB immediately
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute("SELECT is_verified FROM users WHERE email = ?;", ("fatima@gulfdirect.ae",))
    row = cur.fetchone()
    assert row is not None
    assert row["is_verified"] == 1
    conn.close()


def test_login_works_right_after_signup(temp_db):
    """Test that login works immediately right after sign-up."""
    # 1. Register company
    ok_reg, _, sess_reg = register_company_account(
        company_name="Al Noor Logistics LLC",
        trn="100123456789003",
        owner_name="Saeed Al Zaabi",
        email="saeed@alnoor.ae",
        password="ValidPassword123!",
        db_path=temp_db,
    )
    assert ok_reg is True

    # 2. Login immediately
    ok_login, msg_login, sess_login = authenticate_user(
        "saeed@alnoor.ae",
        "ValidPassword123!",
        db_path=temp_db,
    )
    assert ok_login is True
    assert msg_login == "Login successful."
    assert sess_login is not None
    assert sess_login["user_id"] == sess_reg["user_id"]
    assert sess_login["business_id"] == sess_reg["business_id"]
    assert sess_login["email"] == "saeed@alnoor.ae"
    assert sess_login["role"] == "OWNER"


def test_one_time_migration_marks_unverified_as_verified(temp_db):
    """Test one-time migration: any pre-existing unverified accounts become active upon init_db."""
    # Insert an unverified account directly into DB
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute("INSERT INTO businesses (id, name, trn) VALUES (99, 'Legacy Biz', '100999999999003') ON CONFLICT DO NOTHING;")
        conn.execute(
            """
            INSERT INTO users (business_id, username, email, full_name, password_hash, role, is_verified)
            VALUES (99, 'legacy@test.ae', 'legacy@test.ae', 'Legacy Owner', 'hash123', 'OWNER', 0);
            """
        )
    conn.close()

    # Re-run init_db (migration)
    init_db(temp_db)

    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute("SELECT is_verified FROM users WHERE email = 'legacy@test.ae';")
    assert cur.fetchone()["is_verified"] == 1
    conn.close()


def test_forgot_password_shows_contact_team(temp_db):
    """Test forgot password returns instructions to contact team instead of email code flow."""
    ok, msg = request_password_reset("anyone@business.ae", db_path=temp_db)
    assert ok is True
    assert "contact the raseed team" in msg.lower()
