"""Tests for Part 2: Company Accounts and Login (One Account per Company).

Covers:
- Password hashing with bcrypt (plaintext never stored)
- Password strength validation (min 10 chars, letter + number)
- 5-attempt brute-force lockout (15 minutes lockout)
- SIGNUP_CODE enforcement when configured vs omitted
- Multi-tenant data isolation: Company A cannot see Company B's documents,
  credits, approvals, or memory notes
- Cross-tenant document access blocked with PermissionError
- Inactivity session timeout (30 minutes)
"""
import os
import pytest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from auth.company_auth import (
    register_company_account,
    authenticate_user,
    hash_password,
    verify_password,
    validate_password_strength,
    validate_trn_format,
    check_session_timeout,
    LOCKOUT_MINUTES,
    MAX_FAILED_ATTEMPTS,
)
from database.connection import (
    init_db,
    get_db_connection,
    save_chat_message,
    get_chat_history,
    add_memory_note,
    get_memory_notes,
)
from ingest.file_storage import (
    store_uploaded_file,
    read_stored_document,
    generate_presigned_download_url,
)
from tools.write_gateway import stage_action
from tools.read_tools import (
    get_credit_ledger,
    get_staged_actions,
    list_documents,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_p = Path(path)
    init_db(db_p)
    yield db_p
    try:
        os.unlink(path)
    except Exception:
        pass


class TestPasswordSecurity:
    def test_bcrypt_hashing_and_verification(self):
        pwd = "SecurePassword2026!"
        hashed = hash_password(pwd)
        assert hashed != pwd
        assert hashed.startswith("$2b$") or hashed.startswith("$2a$")
        assert verify_password(pwd, hashed) is True
        assert verify_password("WrongPassword123", hashed) is False

    def test_password_strength_validation(self):
        # Too short (< 10)
        ok, msg = validate_password_strength("Short1")
        assert ok is False
        assert "10 characters" in msg

        # No numbers
        ok, msg = validate_password_strength("NoNumbersAtAll!")
        assert ok is False
        assert "number" in msg

        # No letters
        ok, msg = validate_password_strength("123456789012")
        assert ok is False
        assert "letter" in msg

        # Valid strong password
        ok, msg = validate_password_strength("ValidPassw0rd")
        assert ok is True


class TestCompanyRegistrationAndSignupCode:
    def test_trn_format_validation(self):
        # Valid
        ok, res = validate_trn_format("100123456789003")
        assert ok is True
        assert res == "100123456789003"

        # Invalid: doesn't start with 100
        ok, res = validate_trn_format("200123456789003")
        assert ok is False
        assert "100" in res

        # Invalid: wrong length
        ok, res = validate_trn_format("100123456")
        assert ok is False
        assert "15 digits" in res

    def test_signup_code_enforcement(self, temp_db):
        with patch.dict(os.environ, {"SIGNUP_CODE": "SECRET-CODE-XYZ"}):
            # Missing code
            ok, msg, _ = register_company_account(
                company_name="Apex Logistics LLC",
                trn="100999888777001",
                owner_name="Khalid Mansoor",
                email="khalid@apex.ae",
                password="ApexPassword2026",
                signup_code=None,
                db_path=temp_db,
            )
            assert ok is False
            assert "Invalid registration code" in msg

            # Wrong code
            ok, msg, _ = register_company_account(
                company_name="Apex Logistics LLC",
                trn="100999888777001",
                owner_name="Khalid Mansoor",
                email="khalid@apex.ae",
                password="ApexPassword2026",
                signup_code="WRONG-CODE",
                db_path=temp_db,
            )
            assert ok is False
            assert "Invalid registration code" in msg

            # Correct code
            ok, msg, sess = register_company_account(
                company_name="Apex Logistics LLC",
                trn="100999888777001",
                owner_name="Khalid Mansoor",
                email="khalid@apex.ae",
                password="ApexPassword2026",
                signup_code="SECRET-CODE-XYZ",
                db_path=temp_db,
            )
            assert ok is True
            assert sess is not None
            assert sess["company_name"] == "Apex Logistics LLC"
            assert sess["role"] == "OWNER"

    def test_signup_without_signup_code_requirement(self, temp_db):
        with patch.dict(os.environ, {"SIGNUP_CODE": ""}):
            ok, msg, sess = register_company_account(
                company_name="Beacon Trading LLC",
                trn="100555444333002",
                owner_name="Sara Al Hashimi",
                email="sara@beacon.ae",
                password="BeaconPassword2026",
                signup_code=None,
                db_path=temp_db,
            )
            assert ok is True
            assert sess["email"] == "sara@beacon.ae"


class TestAuthenticationAndLockout:
    def test_successful_login_resets_failed_counter(self, temp_db):
        with patch.dict(os.environ, {"SIGNUP_CODE": ""}):
            register_company_account(
                company_name="Crescent LLC",
                trn="100111999888003",
                owner_name="Omar Tariq",
                email="omar@crescent.ae",
                password="CrescentPassword2026",
                db_path=temp_db,
            )

        # 2 failed attempts
        authenticate_user("omar@crescent.ae", "WrongPassword1", db_path=temp_db)
        authenticate_user("omar@crescent.ae", "WrongPassword2", db_path=temp_db)

        # Successful login
        ok, msg, sess = authenticate_user("omar@crescent.ae", "CrescentPassword2026", db_path=temp_db)
        assert ok is True
        assert sess["email"] == "omar@crescent.ae"

        # Verify failed attempts counter was reset to 0
        conn = get_db_connection(temp_db)
        cur = conn.cursor()
        cur.execute("SELECT failed_login_attempts FROM users WHERE email = 'omar@crescent.ae';")
        assert cur.fetchone()["failed_login_attempts"] == 0
        conn.close()

    def test_lockout_after_five_failed_attempts(self, temp_db):
        with patch.dict(os.environ, {"SIGNUP_CODE": ""}):
            register_company_account(
                company_name="Delta Services LLC",
                trn="100222333444004",
                owner_name="Layla Noor",
                email="layla@delta.ae",
                password="DeltaPassword2026",
                db_path=temp_db,
            )

        # Perform 5 consecutive failed attempts
        for i in range(MAX_FAILED_ATTEMPTS):
            ok, msg, _ = authenticate_user("layla@delta.ae", f"WrongGuess_{i}", db_path=temp_db)
            assert ok is False

        # 6th attempt with CORRECT password is still locked out!
        ok, msg, _ = authenticate_user("layla@delta.ae", "DeltaPassword2026", db_path=temp_db)
        assert ok is False
        assert "locked" in msg.lower()
        assert "minute" in msg.lower()


class TestMultiTenantIsolation:
    def test_cross_tenant_data_and_document_isolation(self, temp_db):
        with patch.dict(os.environ, {"SIGNUP_CODE": ""}):
            _, _, sess_a = register_company_account(
                company_name="Company A LLC",
                trn="100111111111001",
                owner_name="Owner A",
                email="a@companya.ae",
                password="PasswordCompanyA2026",
                db_path=temp_db,
            )
            _, _, sess_b = register_company_account(
                company_name="Company B LLC",
                trn="100222222222002",
                owner_name="Owner B",
                email="b@companyb.ae",
                password="PasswordCompanyB2026",
                db_path=temp_db,
            )

        biz_a = sess_a["business_id"]
        biz_b = sess_b["business_id"]
        assert biz_a != biz_b

        # 1. Company A uploads document
        path_a, _, _, doc_id_a = store_uploaded_file(
            file_bytes=b"Company A Tax Invoice AED 10,000",
            filename="inv_a.pdf",
            document_type="invoice",
            business_id=biz_a,
            db_path=temp_db,
        )

        # 2. Company A stores chat & memory note
        save_chat_message(business_id=biz_a, role="user", content="Secret revenue info A", db_path=temp_db)
        add_memory_note(business_id=biz_a, note="Company A specific fact", source="chat", db_path=temp_db)

        # 3. Company A stages an approval action
        stage_action(
            action_type="SUBMIT_REFUND_APPLICATION",
            payload={"period_codes": ["2021-Q1"], "claimed_amount": 2500.0},
            reason="Refund claim for Company A",
            business_id=biz_a,
            db_path=temp_db,
        )

        # --- VERIFY COMPANY B CANNOT SEE ANY OF COMPANY A'S DATA ---
        # Documents
        docs_b = list_documents(business_id=biz_b, db_path=temp_db)
        assert len(docs_b) == 0

        # Chat History
        chat_b = get_chat_history(business_id=biz_b, db_path=temp_db)
        assert len(chat_b) == 0

        # Memory Notes
        mem_b = get_memory_notes(business_id=biz_b, db_path=temp_db)
        assert len(mem_b) == 0

        # Staged Approvals
        staged_b = get_staged_actions(business_id=biz_b, db_path=temp_db)
        assert len(staged_b) == 0

        # Cross-Tenant Direct Storage Read MUST RAISE PermissionError!
        with pytest.raises(PermissionError):
            read_stored_document(storage_path=str(path_a), business_id=biz_b, db_path=temp_db)

        with pytest.raises(PermissionError):
            generate_presigned_download_url(storage_path=str(path_a), business_id=biz_b)

    def test_session_inactivity_timeout(self):
        # Active session (< 30 min)
        recent_time = (datetime.now() - timedelta(minutes=10)).isoformat()
        assert check_session_timeout(recent_time) is False

        # Inactive session (> 30 min)
        stale_time = (datetime.now() - timedelta(minutes=35)).isoformat()
        assert check_session_timeout(stale_time) is True

        # None / Invalid
        assert check_session_timeout(None) is True
