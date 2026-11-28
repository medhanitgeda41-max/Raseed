"""Rate limiting module for Raseed.

Enforces:
- Max 30 chat messages per company per 10 minutes
- Max 10 login attempts per IP / identifier per 10 minutes
- Max 5 sign-ups per IP / identifier per hour
"""
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from database.connection import get_db_connection


CHAT_MAX_MESSAGES = 30
CHAT_WINDOW_SECONDS = 600  # 10 minutes

LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 600  # 10 minutes

SIGNUP_MAX_ACCOUNTS = 5
SIGNUP_WINDOW_SECONDS = 3600  # 1 hour


def record_rate_limit_event(
    key: str,
    event_type: str,
    db_path: Optional[Path] = None,
) -> None:
    """Record an action event for rate limiting."""
    clean_key = str(key).strip().lower()
    clean_type = str(event_type).strip().upper()
    now_str = datetime.now().isoformat()
    try:
        conn = get_db_connection(db_path=db_path, readonly=False)
        with conn:
            conn.execute(
                "INSERT INTO rate_limits (key, event_type, created_at) VALUES (?, ?, ?);",
                (clean_key, clean_type, now_str),
            )
        conn.close()
    except Exception:
        pass


def check_rate_limit(
    key: str,
    event_type: str,
    max_count: int,
    window_seconds: int,
    db_path: Optional[Path] = None,
) -> Tuple[bool, int, str]:
    """Check if action is allowed under rate limit window.
    
    Returns (allowed: bool, retry_after_seconds: int, message: str).
    """
    clean_key = str(key).strip().lower()
    clean_type = str(event_type).strip().upper()
    now = datetime.now()
    window_start = (now - timedelta(seconds=window_seconds)).isoformat()

    try:
        conn = get_db_connection(db_path=db_path, readonly=False)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) as cnt, MIN(created_at) as oldest_in_window
            FROM rate_limits
            WHERE key = ? AND event_type = ? AND created_at >= ?;
            """,
            (clean_key, clean_type, window_start),
        )
        row = cursor.fetchone()
        conn.close()

        count = row["cnt"] if row else 0
        if count >= max_count:
            # Calculate wait time
            retry_after = window_seconds
            if row and row["oldest_in_window"]:
                try:
                    oldest_dt = datetime.fromisoformat(str(row["oldest_in_window"]).replace(" ", "T"))
                    elapsed = (now - oldest_dt).total_seconds()
                    retry_after = max(1, int(window_seconds - elapsed))
                except Exception:
                    pass
            return False, retry_after, f"Rate limit exceeded. Please wait {retry_after} seconds before trying again."

        return True, 0, "OK"
    except Exception:
        # Fallback permissive if rate limit table cannot be queried
        return True, 0, "OK"


def check_chat_rate_limit(
    business_id: int,
    record_if_allowed: bool = True,
    db_path: Optional[Path] = None,
) -> Tuple[bool, int, str]:
    """Max 30 chat messages per company per 10 minutes."""
    key = f"biz_{business_id}"
    allowed, retry_after, msg = check_rate_limit(
        key=key,
        event_type="CHAT_MESSAGE",
        max_count=CHAT_MAX_MESSAGES,
        window_seconds=CHAT_WINDOW_SECONDS,
        db_path=db_path,
    )
    if not allowed:
        return False, retry_after, f"Message limit reached (max 30 messages per 10 minutes). Please wait {retry_after}s."
    if record_if_allowed:
        record_rate_limit_event(key, "CHAT_MESSAGE", db_path=db_path)
    return True, 0, "OK"


def check_login_rate_limit(
    identifier: str,
    record_attempt: bool = True,
    db_path: Optional[Path] = None,
) -> Tuple[bool, int, str]:
    """Max 10 login attempts per identifier per 10 minutes."""
    key = f"login_{identifier.strip().lower()}"
    allowed, retry_after, msg = check_rate_limit(
        key=key,
        event_type="LOGIN_ATTEMPT",
        max_count=LOGIN_MAX_ATTEMPTS,
        window_seconds=LOGIN_WINDOW_SECONDS,
        db_path=db_path,
    )
    if not allowed:
        return False, retry_after, f"Too many login attempts. Please wait {retry_after}s before trying again."
    if record_attempt:
        record_rate_limit_event(key, "LOGIN_ATTEMPT", db_path=db_path)
    return True, 0, "OK"


def check_signup_rate_limit(
    identifier: str,
    record_attempt: bool = True,
    db_path: Optional[Path] = None,
) -> Tuple[bool, int, str]:
    """Max 5 sign-ups per identifier per hour."""
    key = f"signup_{identifier.strip().lower()}"
    allowed, retry_after, msg = check_rate_limit(
        key=key,
        event_type="SIGNUP_ATTEMPT",
        max_count=SIGNUP_MAX_ACCOUNTS,
        window_seconds=SIGNUP_WINDOW_SECONDS,
        db_path=db_path,
    )
    if not allowed:
        return False, retry_after, f"Registration limit reached (max 5 per hour). Please try again later."
    if record_attempt:
        record_rate_limit_event(key, "SIGNUP_ATTEMPT", db_path=db_path)
    return True, 0, "OK"
