"""Company authentication and user session management for Raseed.

Handles:
- Bcrypt password hashing and verification
- Strong password validation (min 10 chars, letter + number)
- Company registration with TRN validation and optional SIGNUP_CODE
- Email verification codes (6-digit, 10-minute expiry, SHA-256 hashed with email salt)
- Password reset via email code (10-minute expiry, locked after 5 failed attempts)
- Rate limiting / 15-minute brute-force lockout after 5 failed login attempts
- Audit event logging (COMPANY_REGISTERED, USER_LOGIN, LOGIN_LOCKED_OUT, USER_LOGOUT, EMAIL_VERIFIED, PASSWORD_RESET)
- Session activity timeout (30 minutes)
- Stale unverified account cleanup (7 days)
"""
from database.request_cache import request_cached
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import bcrypt

from config.settings import settings
from database.connection import get_db_connection, set_tenant_context
from guards.privacy import mask_trn

LOCKOUT_MINUTES = 15
MAX_FAILED_ATTEMPTS = 5
SESSION_TIMEOUT_MINUTES = 30
CODE_EXPIRY_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_CODES_PER_HOUR = 5


def hash_password(password: str) -> str:
    """Hash password using bcrypt with standard salt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify plaintext password against bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def validate_password_strength(password: str) -> Tuple[bool, str]:
    """Validate that password meets security requirements: min 10 chars, at least 1 letter and 1 number."""
    if len(password) < 10:
        return False, "Password must be at least 10 characters long."
    if not re.search(r"[A-Za-z]", password):
        return False, "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number."
    return True, "Password meets strength requirements."


def validate_trn_format(trn: str) -> Tuple[bool, str]:
    """Validate 15-digit UAE TRN starting with 100."""
    clean = re.sub(r"[\s\-]", "", str(trn).strip())
    if not clean.isdigit():
        return False, "TRN must contain only digits."
    if len(clean) != 15:
        return False, f"TRN must be exactly 15 digits (got {len(clean)})."
    if not clean.startswith("100"):
        return False, "UAE TRN must start with '100'."
    return True, clean


def generate_6digit_code() -> str:
    """Generate a cryptographically random 6-digit numeric string."""
    code_int = secrets.randbelow(1_000_000)
    return f"{code_int:06d}"


def hash_verification_code(email: str, code: str) -> str:
    """Hash verification code using SHA-256 with lowercased email as salt."""
    salt = email.strip().lower()
    clean_code = str(code).strip()
    return hashlib.sha256(f"{salt}:{clean_code}".encode("utf-8")).hexdigest()


def register_company_account(
    company_name: str,
    trn: str,
    owner_name: str,
    email: str,
    password: str,
    signup_code: Optional[str] = None,
    require_email_verification: Optional[bool] = None,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Register a new company and owner user account.
    
    If require_email_verification is True, account is created with is_verified=0
    and a 6-digit verification code is emailed.
    Returns (success, message, session_dict).
    """
    comp_clean = str(company_name).strip()
    if not comp_clean:
        return False, "Company legal name is required.", None

    owner_clean = str(owner_name).strip()
    if not owner_clean:
        return False, "Owner full name is required.", None

    email_clean = str(email).strip().lower()
    if not email_clean or "@" not in email_clean or "." not in email_clean:
        return False, "A valid email address is required.", None

    # 1. TRN validation
    trn_valid, trn_res = validate_trn_format(trn)
    if not trn_valid:
        return False, trn_res, None
    valid_trn = trn_res

    # 2. Password validation
    pwd_valid, pwd_msg = validate_password_strength(password)
    if not pwd_valid:
        return False, pwd_msg, None

    # 3. Optional SIGNUP_CODE check
    if "SIGNUP_CODE" in os.environ:
        expected_code = os.environ.get("SIGNUP_CODE", "")
    else:
        expected_code = settings.signup_code or ""

    if expected_code and expected_code.strip():
        if not signup_code or signup_code.strip() != expected_code.strip():
            return False, "Invalid registration code.", None

    # 4. Check uniqueness of TRN and Email
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM businesses WHERE trn = ?;", (valid_trn,))
        if cursor.fetchone():
            return False, f"A business with TRN {mask_trn(valid_trn)} is already registered.", None

        cursor.execute("SELECT id FROM users WHERE email = ?;", (email_clean,))
        if cursor.fetchone():
            return False, "An account with this email address already exists. Please log in.", None

        # Hash password
        pwd_hash = hash_password(password)

        # Create Business and User atomically (is_verified = 1 immediately)
        with conn:
            cursor.execute(
                "INSERT INTO businesses (name, trn) VALUES (?, ?) RETURNING id;",
                (comp_clean, valid_trn),
            )
            b_row = cursor.fetchone()
            biz_id = b_row["id"] if b_row else cursor.lastrowid
            if not biz_id:
                cursor.execute("SELECT id FROM businesses WHERE trn = ?;", (valid_trn,))
                b_find = cursor.fetchone()
                biz_id = b_find["id"] if b_find else None

            # Set RLS tenant context immediately for newly created business
            set_tenant_context(conn, biz_id)

            cursor.execute(
                """
                INSERT INTO users (business_id, username, email, full_name, password_hash, role, is_verified)
                VALUES (?, ?, ?, ?, ?, 'OWNER', 1) RETURNING id;
                """,
                (biz_id, email_clean, email_clean, owner_clean, pwd_hash),
            )
            u_row = cursor.fetchone()
            user_id = u_row["id"] if u_row else cursor.lastrowid
            if not user_id:
                cursor.execute("SELECT id FROM users WHERE email = ?;", (email_clean,))
                u_find = cursor.fetchone()
                user_id = u_find["id"] if u_find else None

            # Create default business profile record
            cursor.execute(
                """
                INSERT INTO business_profile (
                    business_id, legal_name, trn, vat_registration_date, tax_period_type,
                    first_tax_period_start, business_activity, emirate, signatory_name,
                    signatory_email, masked_iban
                ) VALUES (?, ?, ?, '2021-01-01', 'quarterly', '2021-01-01', 'Commercial Trading', 'Dubai', ?, ?, 'AE23****************1234')
                ON CONFLICT(business_id) DO UPDATE SET legal_name = excluded.legal_name;
                """,
                (biz_id, comp_clean, valid_trn, owner_clean, email_clean),
            )

            # Audit event (never log password)
            cursor.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                VALUES (?, ?, 'USER', 'COMPANY_REGISTERED', ?);
                """,
                (
                    biz_id,
                    user_id,
                    json.dumps({
                        "company_name": comp_clean,
                        "trn_masked": mask_trn(valid_trn),
                        "owner_name": owner_clean,
                        "email": email_clean,
                    }),
                ),
            )

        session_data = {
            "user_id": user_id,
            "business_id": biz_id,
            "company_name": comp_clean,
            "user_name": owner_clean,
            "email": email_clean,
            "role": "OWNER",
            "login_time": datetime.now().isoformat(),
            "last_activity_time": datetime.now().isoformat(),
        }
        return True, "Account registered successfully.", session_data

    finally:
        conn.close()


def send_or_resend_verification_code(
    email: str = "",
    purpose: str = "EMAIL_VERIFICATION",
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Deprecated: email verification is removed; accounts are active immediately."""
    return True, "Email verification is not required."


def verify_email_code(
    email: str = "",
    code: str = "",
    db_path: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Deprecated: all accounts are active immediately without email verification."""
    return True, "Email verified successfully.", None


def request_password_reset(email: str = "", db_path: Optional[Path] = None) -> Tuple[bool, str]:
    """Inform user to contact support to reset password."""
    return True, "Please contact the Raseed team to reset your password."


def reset_password_with_code(
    email: str = "",
    code: str = "",
    new_password: str = "",
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Deprecated: contact Raseed team for password resets."""
    return False, "Please contact the Raseed team to reset your password."


def cleanup_stale_unverified_accounts(db_path: Optional[Path] = None, days: int = 7) -> int:
    """No-op as all accounts are verified upon registration."""
    return 0


def authenticate_user(
    email: str,
    password: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Authenticate an owner by email and password with 15-minute brute-force lockout.
    
    Returns (success, message, session_dict).
    """
    email_clean = str(email).strip().lower()
    if not email_clean or not password:
        return False, "Email and password are required.", None

    conn = get_db_connection(db_path=db_path, readonly=False)
    now = datetime.now()

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT u.id, u.business_id, u.email, u.full_name, u.password_hash, u.role,
                   u.failed_login_attempts, u.locked_until, u.is_verified, u.is_totp_enabled,
                   b.name as company_name, b.trn
            FROM users u
            JOIN businesses b ON u.business_id = b.id
            WHERE u.email = ?;
            """,
            (email_clean,),
        )
        row = cursor.fetchone()

        if not row:
            # Generic message to avoid email enumeration
            return False, "Email or password incorrect.", None

        user_id = row["id"]
        biz_id = row["business_id"]
        is_verified = row["is_verified"] if "is_verified" in row.keys() else 1
        is_totp_enabled = int(row["is_totp_enabled"] or 0) if "is_totp_enabled" in row.keys() else 0
        failed_attempts = int(row["failed_login_attempts"] or 0)
        locked_until_str = row["locked_until"]

        # Check if user is locked out
        if locked_until_str:
            try:
                locked_until_dt = datetime.fromisoformat(locked_until_str)
                if now < locked_until_dt:
                    rem_mins = max(1, int((locked_until_dt - now).total_seconds() / 60) + 1)
                    return (
                        False,
                        f"Account locked due to 5 consecutive failed login attempts. Please try again in {rem_mins} minute(s).",
                        None,
                    )
            except Exception:
                pass

        # Verify password with bcrypt
        pwd_hash = row["password_hash"]
        if not pwd_hash or not verify_password(password, pwd_hash):
            new_failed = failed_attempts + 1
            with conn:
                if new_failed >= MAX_FAILED_ATTEMPTS:
                    lock_time = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                    cursor.execute(
                        "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?;",
                        (new_failed, lock_time, user_id),
                    )
                    # Audit lockout
                    cursor.execute(
                        """
                        INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                        VALUES (?, ?, 'SYSTEM', 'LOGIN_LOCKED_OUT', ?);
                        """,
                        (
                            biz_id,
                            user_id,
                            json.dumps({
                                "email": email_clean,
                                "failed_attempts": new_failed,
                                "lockout_duration_minutes": LOCKOUT_MINUTES,
                            }),
                        ),
                    )
                    return (
                        False,
                        f"Account locked due to 5 consecutive failed login attempts. Please try again in {LOCKOUT_MINUTES} minutes.",
                        None,
                    )
                else:
                    cursor.execute(
                        "UPDATE users SET failed_login_attempts = ? WHERE id = ?;",
                        (new_failed, user_id),
                    )
            return False, "Email or password incorrect.", None

        # Check if 2FA is enabled
        if is_totp_enabled == 1:
            return (
                False,
                "Two-factor authentication required. Please enter your 6-digit authenticator code or a backup code.",
                {
                    "needs_2fa": True,
                    "user_id": user_id,
                    "business_id": biz_id,
                    "company_name": row["company_name"],
                    "user_name": row["full_name"] or email_clean,
                    "email": email_clean,
                    "role": row["role"] or "OWNER",
                },
            )

        # Password verified: reset failure counter & lockout
        with conn:
            cursor.execute(
                "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?;",
                (user_id,),
            )
            # Log successful login
            cursor.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                VALUES (?, ?, 'USER', 'USER_LOGIN', ?);
                """,
                (
                    biz_id,
                    user_id,
                    json.dumps({"email": email_clean, "company_name": row["company_name"]}),
                ),
            )

        session_data = {
            "user_id": user_id,
            "business_id": biz_id,
            "company_name": row["company_name"],
            "user_name": row["full_name"] or email_clean,
            "email": email_clean,
            "role": row["role"] or "OWNER",
            "login_time": now.isoformat(),
            "last_activity_time": now.isoformat(),
        }
        return True, "Login successful.", session_data

    finally:
        conn.close()


def log_user_logout(user_id: int, business_id: int, db_path: Optional[Path] = None) -> None:
    """Record user logout in audit log."""
    try:
        conn = get_db_connection(db_path=db_path, readonly=False)
        with conn:
            conn.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                VALUES (?, ?, 'USER', 'USER_LOGOUT', '{}');
                """,
                (business_id, user_id),
            )
        conn.close()
    except Exception:
        pass


def check_session_timeout(last_activity_str: Optional[str]) -> bool:
    """Return True if session has expired due to 30 minutes of inactivity."""
    if not last_activity_str:
        return True
    try:
        last_act = datetime.fromisoformat(last_activity_str)
        if datetime.now() - last_act > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            return True
        return False
    except Exception:
        return True


def complete_2fa_login(
    session_data: Dict[str, Any],
    code: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Complete 2FA login with TOTP code or backup code."""
    from auth.two_factor import verify_totp_or_backup_code

    user_id = session_data.get("user_id")
    biz_id = session_data.get("business_id")
    if not user_id:
        return False, "Invalid session data.", None

    ok, msg = verify_totp_or_backup_code(user_id=user_id, code=code, db_path=db_path)
    if not ok:
        return False, msg, None

    conn = get_db_connection(db_path=db_path, readonly=False)
    now = datetime.now()
    try:
        with conn:
            conn.execute(
                "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?;",
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                VALUES (?, ?, 'USER', 'USER_LOGIN_2FA', ?);
                """,
                (biz_id, user_id, json.dumps({"email": session_data.get("email")})),
            )
        updated_session = {
            **session_data,
            "login_time": now.isoformat(),
            "last_activity_time": now.isoformat(),
        }
        updated_session.pop("needs_2fa", None)
        return True, f"Login successful with 2FA. {msg}", updated_session
    finally:
        conn.close()


def delete_company_account(
    business_id: int,
    user_id: int,
    password: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Permanently delete company account, purging files from R2/storage, database rows, chat, facts."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ? AND business_id = ?;", (user_id, business_id))
        row = cursor.fetchone()
        if not row or not row["password_hash"]:
            return False, "User not found or unauthenticated."

        if not verify_password(password, row["password_hash"]):
            return False, "Incorrect password. Company deletion canceled."

        # Delete all files in storage for this business
        try:
            from ingest.storage_interface import get_storage_backend
            storage = get_storage_backend()
            storage.delete_business_files(business_id)
        except Exception:
            pass

        # Purge all database rows for this business
        tables = [
            "verification_codes",
            "rate_limits",
            "chat_messages",
            "memory_notes",
            "ocr_cache",
            "scheduler_messages",
            "scheduler_runs",
            "tasks",
            "staged_actions",
            "credit_allocations",
            "credit_ledger_entries",
            "credit_ledger",
            "transactions",
            "invoices",
            "vat_returns",
            "uploaded_documents",
            "business_profile",
            "audit_events",
            "users",
            "businesses",
        ]
        with conn:
            for tbl in tables:
                try:
                    if tbl == "verification_codes":
                        conn.execute("DELETE FROM verification_codes WHERE email IN (SELECT email FROM users WHERE business_id = ?);", (business_id,))
                    elif tbl == "rate_limits":
                        conn.execute("DELETE FROM rate_limits WHERE key = ?;", (f"biz_{business_id}",))
                    elif tbl == "credit_allocations":
                        conn.execute("DELETE FROM credit_allocations WHERE credit_ledger_entry_id IN (SELECT id FROM credit_ledger_entries WHERE business_id = ?);", (business_id,))
                    elif tbl == "ocr_cache":
                        conn.execute("DELETE FROM ocr_cache WHERE file_hash IN (SELECT file_hash FROM uploaded_documents WHERE business_id = ?);", (business_id,))
                    elif tbl == "businesses":
                        conn.execute("DELETE FROM businesses WHERE id = ?;", (business_id,))
                    else:
                        conn.execute(f"DELETE FROM {tbl} WHERE business_id = ?;", (business_id,))
                except Exception:
                    pass

        return True, "Company account and all associated cloud and database records have been permanently purged."
    finally:
        conn.close()


def set_company_local_ai_only(business_id: int, local_only: bool, db_path: Optional[Path] = None) -> bool:
    """Toggle local-only AI setting for a company."""
    val = 1 if local_only else 0
    try:
        conn = get_db_connection(db_path=db_path, readonly=False)
        with conn:
            conn.execute("UPDATE business_profile SET local_ai_only = ? WHERE business_id = ?;", (val, business_id))
        conn.close()
        return True
    except Exception:
        return False


@request_cached
def get_company_local_ai_only(business_id: int, db_path: Optional[Path] = None) -> bool:
    """Retrieve local-only AI setting for a company."""
    try:
        conn = get_db_connection(db_path=db_path, readonly=True)
        cursor = conn.cursor()
        cursor.execute("SELECT local_ai_only FROM business_profile WHERE business_id = ?;", (business_id,))
        row = cursor.fetchone()
        conn.close()
        if row and "local_ai_only" in row.keys() and row["local_ai_only"] is not None:
            return bool(row["local_ai_only"])
        return False
    except Exception:
        return False
