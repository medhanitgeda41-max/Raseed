"""Two-Factor Authentication (2FA) with TOTP and Backup Codes.

Uses pyotp for standard RFC 6238 TOTP and qrcode for QR code generation.
Provides 8 one-time backup codes stored hashed (never plain text).
"""
import base64
import hashlib
import io
import json
import secrets
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pyotp
import qrcode

from database.connection import get_db_connection
from auth.company_auth import verify_password


def hash_backup_code(code: str) -> str:
    """Hash a backup code using SHA-256."""
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def generate_totp_setup(email: str, company_name: str = "Raseed") -> Dict[str, Any]:
    """Generate TOTP secret, provisioning URI, QR code data URI, and 8 one-time backup codes."""
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    issuer = f"Raseed ({company_name})" if company_name != "Raseed" else "Raseed"
    uri = totp.provisioning_uri(name=email, issuer_name=issuer)

    # Generate QR Code image as base64 PNG data URI
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    qr_data_uri = f"data:image/png;base64,{qr_b64}"

    # Generate 8 random backup codes (8 characters each, uppercase alphanumeric)
    plain_backup_codes = []
    hashed_backup_codes = []
    for _ in range(8):
        code = secrets.token_hex(4).upper()
        plain_backup_codes.append(code)
        hashed_backup_codes.append(hash_backup_code(code))

    return {
        "secret": secret,
        "uri": uri,
        "qr_data_uri": qr_data_uri,
        "plain_backup_codes": plain_backup_codes,
        "backup_codes_json": json.dumps(hashed_backup_codes),
    }


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code against a secret."""
    try:
        clean_code = str(code).strip().replace(" ", "")
        totp = pyotp.TOTP(secret)
        return bool(totp.verify(clean_code, valid_window=1))
    except Exception:
        return False


def enable_totp(
    user_id: int,
    secret: str,
    verification_code: str,
    backup_codes_json: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Verify the initial 6-digit code and activate 2FA for the user."""
    if not verify_totp_code(secret, verification_code):
        return False, "Invalid 6-digit authenticator code. Please check your app and try again."

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            conn.execute(
                """
                UPDATE users
                SET is_totp_enabled = 1, totp_secret = ?, backup_codes_json = ?
                WHERE id = ?;
                """,
                (secret, backup_codes_json, user_id),
            )
        return True, "Two-factor authentication (2FA) has been successfully enabled."
    finally:
        conn.close()


def disable_totp(
    user_id: int,
    password: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Disable 2FA for the user after verifying current password."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ?;", (user_id,))
        row = cursor.fetchone()
        if not row or not row["password_hash"]:
            return False, "User not found."

        if not verify_password(password, row["password_hash"]):
            return False, "Incorrect password. Cannot disable 2FA."

        with conn:
            conn.execute(
                """
                UPDATE users
                SET is_totp_enabled = 0, totp_secret = NULL, backup_codes_json = NULL
                WHERE id = ?;
                """,
                (user_id,),
            )
        return True, "Two-factor authentication (2FA) has been disabled."
    finally:
        conn.close()


def verify_totp_or_backup_code(
    user_id: int,
    code: str,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Verify either a 6-digit TOTP code or one of the 8 one-time backup codes."""
    clean_code = str(code).strip().replace(" ", "").upper()
    if not clean_code:
        return False, "Verification code is required."

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT totp_secret, backup_codes_json, is_totp_enabled FROM users WHERE id = ?;",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row or not row["is_totp_enabled"]:
            return False, "2FA is not enabled for this account."

        secret = row["totp_secret"]
        backup_json = row["backup_codes_json"]

        # 1. Try TOTP code
        if len(clean_code) == 6 and clean_code.isdigit() and secret:
            if verify_totp_code(secret, clean_code):
                return True, "TOTP code verified successfully."

        # 2. Try backup code
        if backup_json:
            try:
                hashed_list: List[str] = json.loads(backup_json)
                code_hash = hash_backup_code(clean_code)
                if code_hash in hashed_list:
                    # Consume the backup code (single-use)
                    hashed_list.remove(code_hash)
                    with conn:
                        conn.execute(
                            "UPDATE users SET backup_codes_json = ? WHERE id = ?;",
                            (json.dumps(hashed_list), user_id),
                        )
                    rem = len(hashed_list)
                    return True, f"Backup code accepted. ({rem} backup code(s) remaining)."
            except Exception:
                pass

        return False, "Invalid 2FA code or backup code."
    finally:
        conn.close()
