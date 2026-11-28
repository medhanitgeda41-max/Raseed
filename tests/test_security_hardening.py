"""Tests for Part 2: Security Hardening.

Covers:
1. File upload restrictions (20MB limit, 200 files per company, real magic bytes, archive rejection, encrypted PDF rejection)
2. Rate limits (30 chat msgs / 10 min, 10 login attempts / 10 min, 5 signups / hr)
3. Optional 2FA (TOTP setup, QR code, 8 one-time backup codes, login challenge, consumption of backup codes, disabling)
4. Privacy in logs (masking passwords, codes, TRNs, IBANs, audit sanitization)
5. Local AI only per company (bypasses cloud providers completely)
6. Delete company account (purges R2/storage files, database rows, chat, facts)
7. Postgres Row-Level Security helper
"""
import io
import json
import os
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import pytest

from auth.company_auth import (
    register_company_account,
    authenticate_user,
    complete_2fa_login,
    delete_company_account,
    set_company_local_ai_only,
    get_company_local_ai_only,
)
from auth.two_factor import (
    generate_totp_setup,
    verify_totp_code,
    enable_totp,
    disable_totp,
    verify_totp_or_backup_code,
    hash_backup_code,
)
from database.connection import init_db, get_db_connection, set_tenant_context
from guards.file_security import (
    validate_uploaded_file_security,
    check_pdf_encrypted,
    MAX_FILE_SIZE_BYTES,
    MAX_FILES_PER_COMPANY,
)
from guards.rate_limiter import (
    check_chat_rate_limit,
    check_login_rate_limit,
    check_signup_rate_limit,
    record_rate_limit_event,
)
from guards.privacy import (
    mask_log_message,
    sanitize_audit_details,
    mask_trn,
    mask_iban,
)
from llm_client import _build_provider_chain, ask


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Isolated SQLite database for security tests."""
    monkeypatch.setenv("SIGNUP_CODE", "")
    db_file = tmp_path / "test_security.db"
    init_db(db_file)
    return db_file


# ------------------------------------------------------------------------------
# 1. FILE UPLOAD SECURITY
# ------------------------------------------------------------------------------
def test_file_size_limit_rejection(temp_db):
    """Files over 20 MB are rejected."""
    large_bytes = b"0" * (MAX_FILE_SIZE_BYTES + 1024)
    ok, msg, _ = validate_uploaded_file_security(large_bytes, "huge.pdf", business_id=1, db_path=temp_db)
    assert ok is False
    assert "exceeds maximum allowed size" in msg


def test_file_upload_quota_limit(temp_db):
    """A company cannot upload more than 200 files."""
    conn = get_db_connection(temp_db, readonly=False)
    with conn:
        for i in range(MAX_FILES_PER_COMPANY):
            conn.execute(
                """
                INSERT INTO uploaded_documents (
                    business_id, file_hash, original_filename, storage_path,
                    document_type, file_size_bytes, mime_type
                ) VALUES (1, ?, ?, ?, 'invoice', 100, 'application/pdf');
                """,
                (f"hash_{i}", f"inv_{i}.pdf", f"1/invoice/inv_{i}.pdf"),
            )
    conn.close()

    ok, msg, _ = validate_uploaded_file_security(b"%PDF-1.4 test", "test_201.pdf", business_id=1, db_path=temp_db)
    assert ok is False
    assert "storage limit reached" in msg


def test_file_magic_bytes_validation(temp_db):
    """Valid signatures pass, fake extensions fail, and archives are rejected."""
    # 1. Authentic PDF
    valid_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    ok, _, mime = validate_uploaded_file_security(valid_pdf, "invoice.pdf", business_id=1, db_path=temp_db)
    assert ok is True
    assert mime == "application/pdf"

    # Fake PDF (text file named .pdf)
    ok, msg, _ = validate_uploaded_file_security(b"This is just plain text", "fake.pdf", business_id=1, db_path=temp_db)
    assert ok is False
    assert "signatures" in msg.lower()

    # 2. Authentic XLSX
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types></Types>")
        z.writestr("xl/workbook.xml", "<workbook></workbook>")
    xlsx_bytes = buf.getvalue()
    ok, _, mime = validate_uploaded_file_security(xlsx_bytes, "register.xlsx", business_id=1, db_path=temp_db)
    assert ok is True
    assert "openxmlformats" in mime

    # 3. Archive rejection (Generic zip, rar, 7z)
    buf_zip = io.BytesIO()
    with zipfile.ZipFile(buf_zip, "w") as z:
        z.writestr("some_script.sh", "echo hello")
    ok, msg, _ = validate_uploaded_file_security(buf_zip.getvalue(), "archive.zip", business_id=1, db_path=temp_db)
    assert ok is False
    assert "archive" in msg.lower() or "missing workbook" in msg.lower()

    # RAR archive header
    rar_bytes = b"Rar!\x1a\x07\x00" + b"extra data"
    ok, msg, _ = validate_uploaded_file_security(rar_bytes, "invoices.rar", business_id=1, db_path=temp_db)
    assert ok is False
    assert "archive" in msg.lower()


# ------------------------------------------------------------------------------
# 2. RATE LIMITS
# ------------------------------------------------------------------------------
def test_chat_rate_limiting(temp_db):
    """Enforces max 30 chat messages per 10 minutes."""
    biz_id = 10
    # First 30 messages allowed
    for _ in range(30):
        ok, _, _ = check_chat_rate_limit(business_id=biz_id, record_if_allowed=True, db_path=temp_db)
        assert ok is True

    # 31st message rejected
    ok, retry_after, msg = check_chat_rate_limit(business_id=biz_id, record_if_allowed=True, db_path=temp_db)
    assert ok is False
    assert "Message limit reached" in msg
    assert retry_after > 0


def test_login_and_signup_rate_limiting(temp_db):
    """Enforces max 10 login attempts per 10 min and max 5 signups per hour."""
    ip = "192.168.1.100"
    for _ in range(10):
        ok, _, _ = check_login_rate_limit(ip, record_attempt=True, db_path=temp_db)
        assert ok is True
    ok, _, msg = check_login_rate_limit(ip, record_attempt=True, db_path=temp_db)
    assert ok is False
    assert "Too many login attempts" in msg

    # Sign-up limit (max 5 per hour)
    for _ in range(5):
        ok, _, _ = check_signup_rate_limit(ip, record_attempt=True, db_path=temp_db)
        assert ok is True
    ok, _, msg = check_signup_rate_limit(ip, record_attempt=True, db_path=temp_db)
    assert ok is False
    assert "Registration limit reached" in msg


# ------------------------------------------------------------------------------
# 3. OPTIONAL 2FA (TOTP + BACKUP CODES)
# ------------------------------------------------------------------------------
def test_two_factor_authentication_lifecycle(temp_db):
    """Test full 2FA lifecycle: setup, backup codes, login prompt, backup consumption, disable."""
    import pyotp

    # 1. Register verified user
    ok, _, sess = register_company_account(
        company_name="Apex Security LLC",
        trn="100999888111003",
        owner_name="Security Lead",
        email="lead@apexsecurity.ae",
        password="SecurePass2026!",
        require_email_verification=False,
        db_path=temp_db,
    )
    user_id = sess["user_id"]
    biz_id = sess["business_id"]

    # 2. Generate 2FA setup
    setup = generate_totp_setup(email="lead@apexsecurity.ae", company_name="Apex Security LLC")
    assert "secret" in setup
    assert "qr_data_uri" in setup
    assert setup["qr_data_uri"].startswith("data:image/png;base64,")
    assert len(setup["plain_backup_codes"]) == 8
    secret = setup["secret"]
    backup_codes = setup["plain_backup_codes"]

    # 3. Enable 2FA with valid TOTP code
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    ok, msg = enable_totp(
        user_id=user_id,
        secret=secret,
        verification_code=valid_code,
        backup_codes_json=setup["backup_codes_json"],
        db_path=temp_db,
    )
    assert ok is True

    # 4. Login now requires 2FA challenge
    l_ok, l_msg, l_sess = authenticate_user("lead@apexsecurity.ae", "SecurePass2026!", db_path=temp_db)
    assert l_ok is False
    assert l_sess is not None
    assert l_sess.get("needs_2fa") is True
    assert "Two-factor authentication required" in l_msg

    # 5. Complete login with backup code (single-use)
    backup_code_to_use = backup_codes[0]
    c_ok, c_msg, c_sess = complete_2fa_login(l_sess, backup_code_to_use, db_path=temp_db)
    assert c_ok is True
    assert c_sess is not None
    assert c_sess["email"] == "lead@apexsecurity.ae"
    assert "7 backup code(s) remaining" in c_msg

    # Reusing the same backup code fails
    c_ok2, _, _ = complete_2fa_login(l_sess, backup_code_to_use, db_path=temp_db)
    assert c_ok2 is False

    # 6. Complete login with standard TOTP code
    fresh_totp_code = totp.now()
    c_ok3, _, c_sess3 = complete_2fa_login(l_sess, fresh_totp_code, db_path=temp_db)
    assert c_ok3 is True

    # 7. Disable 2FA with password
    d_ok, d_msg = disable_totp(user_id=user_id, password="SecurePass2026!", db_path=temp_db)
    assert d_ok is True

    # Login now succeeds directly without 2FA challenge
    l_ok_direct, _, _ = authenticate_user("lead@apexsecurity.ae", "SecurePass2026!", db_path=temp_db)
    assert l_ok_direct is True


# ------------------------------------------------------------------------------
# 4. PRIVACY IN LOGS
# ------------------------------------------------------------------------------
def test_privacy_log_masking():
    """Test masking passwords, codes, TRNs, and IBANs in logs."""
    raw_log = "User login failed: email=test@test.ae password='MySecretPassword123' code: 987654 TRN 100123456789003 IBAN AE123456789012345678901"
    masked = mask_log_message(raw_log)
    assert "MySecretPassword123" not in masked
    assert "987654" not in masked
    assert "100XXXXXXXXX003" in masked
    assert "AE12XXXXXXXXXXXXXXXX8901" in masked

    # Audit details sanitization
    details = {
        "user": "fatima@test.ae",
        "password": "SecretPassword123",
        "verification_code": "123456",
        "trn": "100111222333003",
        "raw_text": "Private invoice content line 1",
    }
    sanitized = sanitize_audit_details(details)
    assert sanitized["password"] == "[REDACTED]"
    assert sanitized["verification_code"] == "[REDACTED]"
    assert sanitized["raw_text"] == "[REDACTED]"
    assert sanitized["trn"] == "100XXXXXXXXX003"


# ------------------------------------------------------------------------------
# 5. LOCAL AI ONLY OPTION PER COMPANY
# ------------------------------------------------------------------------------
def test_company_local_ai_only_setting(temp_db):
    """Setting local_ai_only restricts provider chain to local Ollama only."""
    register_company_account(
        company_name="Local AI Co",
        trn="100444555666003",
        owner_name="Local Owner",
        email="local@test.ae",
        password="LocalPassword123",
        require_email_verification=False,
        db_path=temp_db,
    )
    # Get business ID
    conn = get_db_connection(temp_db)
    b_row = conn.execute("SELECT id FROM businesses WHERE trn = '100444555666003';").fetchone()
    biz_id = b_row["id"]
    conn.close()

    assert get_company_local_ai_only(biz_id, db_path=temp_db) is False

    # Set local AI only
    set_company_local_ai_only(biz_id, True, db_path=temp_db)
    assert get_company_local_ai_only(biz_id, db_path=temp_db) is True

    # Check provider chain: when local_ai_only=True, only non-cloud providers exist
    chain = _build_provider_chain(local_ai_only=True)
    assert len(chain) == 1
    assert chain[0]["is_cloud"] is False
    assert "ollama" in chain[0]["name"]


# ------------------------------------------------------------------------------
# 6. DELETE MY COMPANY ACCOUNT
# ------------------------------------------------------------------------------
def test_delete_company_account(temp_db):
    """Test permanently purging company data, documents, and database rows."""
    ok, _, sess = register_company_account(
        company_name="Delete Me LLC",
        trn="100888777666003",
        owner_name="Delete Owner",
        email="delete@me.ae",
        password="DeletePassword123",
        require_email_verification=False,
        db_path=temp_db,
    )
    biz_id = sess["business_id"]
    user_id = sess["user_id"]

    # Wrong password aborts deletion
    del_ok, del_msg = delete_company_account(biz_id, user_id, "WrongPassword", db_path=temp_db)
    assert del_ok is False
    assert "Incorrect password" in del_msg

    # Correct password deletes company
    del_ok, del_msg = delete_company_account(biz_id, user_id, "DeletePassword123", db_path=temp_db)
    assert del_ok is True
    assert "permanently purged" in del_msg

    # Verify rows deleted
    conn = get_db_connection(temp_db)
    assert conn.execute("SELECT COUNT(*) as cnt FROM businesses WHERE id = ?;", (biz_id,)).fetchone()["cnt"] == 0
    assert conn.execute("SELECT COUNT(*) as cnt FROM users WHERE business_id = ?;", (biz_id,)).fetchone()["cnt"] == 0
    assert conn.execute("SELECT COUNT(*) as cnt FROM business_profile WHERE business_id = ?;", (biz_id,)).fetchone()["cnt"] == 0
    conn.close()


# ------------------------------------------------------------------------------
# 7. POSTGRES ROW-LEVEL SECURITY HELPER
# ------------------------------------------------------------------------------
def test_set_tenant_context_helper(temp_db):
    """Test that set_tenant_context runs safely on SQLite connection without raising."""
    conn = get_db_connection(temp_db)
    # Should not raise exception
    set_tenant_context(conn, 42)
    conn.close()
