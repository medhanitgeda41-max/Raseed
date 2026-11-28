"""End-to-End Verification of all Part 4 checks:
- Email verification & lockout
- Password reset
- File security (size >20MB, archive .zip)
- Chat rate limiting (31st message blocked)
- 2FA setup, QR code, TOTP verification, and backup code login
- Local AI Only mode (zero cloud calls)
- Account deletion data purge
- Column matching (Arabic headers, typos fuzzy-match, swapped Amount/VAT auto-fix, cached pattern recognition)
"""
import io
import json
import pyotp
import pytest
import pandas as pd
from pathlib import Path
from unittest.mock import patch, MagicMock

from database.connection import init_db, get_db_connection
from auth.company_auth import (
    register_company_account,
    authenticate_user,
    verify_email_code,
    request_password_reset,
    reset_password_with_code,
    complete_2fa_login,
    set_company_local_ai_only,
    get_company_local_ai_only,
    delete_company_account,
)
from auth.two_factor import generate_totp_setup, enable_totp
from guards.file_security import validate_uploaded_file_security
from guards.rate_limiter import check_chat_rate_limit
from ingest.excel_parser import (
    guess_column_mapping,
    detect_swapped_amount_and_vat,
    save_cached_column_mapping,
    get_cached_column_mapping,
    parse_excel_register,
)
import llm_client


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "part4_e2e.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("SIGNUP_CODE", "")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    init_db(db_path=db_file)
    return db_file


def test_e2e_email_verification_and_lockout(clean_db, monkeypatch):
    """Check: Sign-up is active immediately; 5 failed login attempts trigger lockout."""
    # 1. Signup directly active
    ok, msg, session = register_company_account(
        company_name="Al Amal Trading LLC",
        trn="100123456789003",
        owner_name="Fatima Al Zaabi",
        email="fatima@alamal.ae",
        password="ValidPassword123!",
        db_path=clean_db,
    )
    assert ok is True
    assert session is not None
    assert session.get("unverified") is not True

    # 2. Login succeeds immediately
    ok_l, msg_l, sess_l = authenticate_user("fatima@alamal.ae", "ValidPassword123!", db_path=clean_db)
    assert ok_l is True
    assert sess_l["email"] == "fatima@alamal.ae"

    # 3. Test 5 wrong password attempts -> lockout
    for _ in range(4):
        ok_fail, msg_fail, _ = authenticate_user("fatima@alamal.ae", "WrongPassword123!", db_path=clean_db)
        assert ok_fail is False
        assert "incorrect" in msg_fail.lower()

    ok_locked, msg_locked, _ = authenticate_user("fatima@alamal.ae", "WrongPassword123!", db_path=clean_db)
    assert ok_locked is False
    assert "locked" in msg_locked.lower()


def test_e2e_password_reset_flow(clean_db, monkeypatch):
    """Check: Forgot password directs user to contact Raseed team."""
    ok_req, msg_req = request_password_reset("rashid@gulfretail.ae", db_path=clean_db)
    assert ok_req is True
    assert "contact the raseed team" in msg_req.lower()


def test_e2e_file_security_and_chat_rate_limiting(clean_db):
    """Check 4, 5, 6: Upload >20MB rejected; .zip rejected; 31st chat message blocked."""
    # Check 4: >20MB rejected
    huge_bytes = b"%PDF-" + b"0" * (20 * 1024 * 1024 + 50)
    ok_huge, msg_huge, _ = validate_uploaded_file_security(huge_bytes, "huge.pdf", business_id=1, db_path=clean_db)
    assert ok_huge is False
    assert "exceeds" in msg_huge

    # Check 5: .zip archive rejected
    zip_bytes = b"PK\x03\x04\x14\x00\x00\x00\x08\x00dummyzipcontent"
    ok_zip, msg_zip, _ = validate_uploaded_file_security(zip_bytes, "invoices.zip", business_id=1, db_path=clean_db)
    assert ok_zip is False
    assert "archive" in msg_zip.lower()

    # Check 6: 31 chat messages in 10 min -> 31st rate-limited
    for m in range(30):
        ok, remaining, _ = check_chat_rate_limit(business_id=101, db_path=clean_db)
        assert ok is True
    # 31st message must be blocked
    ok_31, remaining_31, msg_31 = check_chat_rate_limit(business_id=101, db_path=clean_db)
    assert ok_31 is False
    assert "limit reached" in msg_31.lower()


def test_e2e_two_factor_auth_lifecycle(clean_db):
    """Check 7: 2FA setup -> QR code -> verify OTP -> enabled -> login asks for OTP -> backup code works."""
    ok, _, session = register_company_account(
        company_name="Secure Tech LLC",
        trn="100111222333003",
        owner_name="Mariam Al Nuaimi",
        email="mariam@securetech.ae",
        password="SecurePassword123!",
        require_email_verification=False,
        db_path=clean_db,
    )
    user_id = session["user_id"]
    conn = get_db_connection(db_path=clean_db)
    conn.execute("UPDATE users SET is_verified = 1 WHERE id = ?;", (user_id,))
    conn.commit()
    conn.close()

    # 1. Generate 2FA setup
    setup = generate_totp_setup(email="mariam@securetech.ae", company_name="Secure Tech LLC")
    assert setup["secret"]
    assert "data:image/png;base64," in setup["qr_data_uri"]
    assert len(setup["plain_backup_codes"]) == 8
    secret = setup["secret"]
    first_backup_code = setup["plain_backup_codes"][0]

    # 2. Verify with valid TOTP and enable
    valid_code = pyotp.TOTP(secret).now()
    ok_enable, msg = enable_totp(
        user_id=user_id,
        secret=secret,
        verification_code=valid_code,
        backup_codes_json=setup["backup_codes_json"],
        db_path=clean_db,
    )
    assert ok_enable is True

    # 3. Login now requires 2FA
    ok_log, msg_log, sess_log = authenticate_user("mariam@securetech.ae", "SecurePassword123!", db_path=clean_db)
    assert ok_log is False
    assert sess_log is not None
    assert sess_log.get("needs_2fa") is True
    assert "Two-factor authentication required" in msg_log

    # 4. Complete login using single-use backup code
    ok_2fa, msg_2fa, sess_2fa = complete_2fa_login(
        session_data=sess_log,
        code=first_backup_code,
        db_path=clean_db,
    )
    assert ok_2fa is True
    assert "7 backup code(s) remaining" in msg_2fa

    # 5. Same backup code cannot be used again
    ok_reuse, _, _ = complete_2fa_login(
        session_data=sess_log,
        code=first_backup_code,
        db_path=clean_db,
    )
    assert ok_reuse is False


def test_e2e_local_ai_only_and_account_deletion(clean_db):
    """Check 8 & 9: Local AI Only toggle -> zero cloud calls; delete company account -> data purge."""
    ok, _, session = register_company_account(
        company_name="Private Defense LLC",
        trn="100999888777003",
        owner_name="Sultan Al Dhaheri",
        email="sultan@defense.ae",
        password="DefensePassword123!",
        require_email_verification=False,
        db_path=clean_db,
    )
    biz_id = session["business_id"]
    user_id = session["user_id"]

    # Check 8: Local AI Only
    assert get_company_local_ai_only(biz_id, db_path=clean_db) is False
    set_company_local_ai_only(biz_id, True, db_path=clean_db)
    assert get_company_local_ai_only(biz_id, db_path=clean_db) is True

    # Verify LLM provider chain excludes cloud providers when local_ai_only=True
    chain = llm_client._build_provider_chain(local_ai_only=True)
    cloud_providers = [p for p in chain if p.get("is_cloud") is True]
    assert len(cloud_providers) == 0

    resp = llm_client.ask([{"role": "user", "content": "What is UAE VAT rate?"}], local_ai_only=True)
    assert resp.content
    assert resp.provider in ("ollama", "offline_rules", "offline_deterministic")

    # Check 9: Delete company account and confirm total purge
    ok_del, msg_del = delete_company_account(biz_id, user_id=user_id, password="DefensePassword123!", db_path=clean_db)
    assert ok_del is True

    # Confirm login is now impossible
    ok_log, _, _ = authenticate_user("sultan@defense.ae", "DefensePassword123!", db_path=clean_db)
    assert ok_log is False

    # Confirm business and user records are deleted
    conn = get_db_connection(db_path=clean_db)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM businesses WHERE id = ?;", (biz_id,))
    assert cursor.fetchone() is None
    cursor.execute("SELECT id FROM users WHERE id = ?;", (user_id,))
    assert cursor.fetchone() is None
    conn.close()


def test_e2e_column_matching_features(clean_db):
    """Check 10, 11, 12, 13:
    10. Upload register with Arabic headers -> correctly mapped
    11. Headers with typos -> fuzzy-matched
    12. Swapped Amount/VAT -> detected and fixed
    13. Save mapping -> next upload recognized instantly
    """
    # 10. Arabic headers
    arabic_headers = ["رقم الفاتورة", "تاريخ الفاتورة", "المبلغ الخاضع للضريبة", "مبلغ الضريبة", "المبلغ الإجمالي"]
    ar_map, ar_conf, ar_unmapped = guess_column_mapping(arabic_headers)
    assert ar_map["invoice_number"] == "رقم الفاتورة"
    assert ar_map["taxable_amount"] == "المبلغ الخاضع للضريبة"
    assert ar_map["vat_amount"] == "مبلغ الضريبة"
    assert ar_conf == 1.0

    # 11. Headers with typos -> fuzzy matched
    typo_headers = ["invoce num", "taxble val", "input vat", "transac date"]
    fz_map, fz_conf, fz_unmapped = guess_column_mapping(typo_headers)
    assert fz_map["invoice_number"] == "invoce num"
    assert fz_map["taxable_amount"] == "taxble val"
    assert fz_map["vat_amount"] == "input vat"

    # 12. Swapped Amount/VAT detected and fixed
    swapped_df = pd.DataFrame({
        "invoice_number": ["INV-1", "INV-2", "INV-3"],
        "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "taxable_amount": [5.0, 10.0, 25.0],        # Swapped! (5% of 100, 200, 500)
        "vat_amount": [100.0, 200.0, 500.0],       # Swapped! (Real taxable)
    })
    assert detect_swapped_amount_and_vat(swapped_df, "taxable_amount", "vat_amount") is True
    sw_map, sw_conf, _ = guess_column_mapping(list(swapped_df.columns), df=swapped_df)
    assert sw_map["taxable_amount"] == "vat_amount"
    assert sw_map["vat_amount"] == "taxable_amount"

    # 13. Save mapping -> next upload recognized instantly
    biz_id = 99
    conn = get_db_connection(db_path=clean_db)
    with conn:
        conn.execute("INSERT OR IGNORE INTO businesses (id, name, trn) VALUES (99, 'Custom Format LLC', '100123456789003');")
    conn.close()

    custom_headers = ["Col_1", "Col_2", "Col_3", "Col_4"]
    user_mapping = {
        "invoice_number": "Col_1",
        "date": "Col_2",
        "taxable_amount": "Col_3",
        "vat_amount": "Col_4",
    }
    save_cached_column_mapping(biz_id, custom_headers, user_mapping, db_path=clean_db)

    # Re-upload with same headers recognizes instantly with 1.0 confidence
    inst_map, inst_conf, inst_unmapped = guess_column_mapping(custom_headers, business_id=biz_id, db_path=clean_db)
    assert inst_map == user_mapping
    assert inst_conf == 1.0
    assert len(inst_unmapped) == 0
