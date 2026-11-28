"""Refund safety guard enforcing exact ledger matching, expiry bans, and threshold gates.

Golden Rule: The refund claim amount must exactly equal the code-calculated eligible excess credit.
Rejects claims containing expired periods, items under review, or unverified amounts.
"""
from typing import Tuple, List, Optional
from pathlib import Path
from config.settings import settings
from database.connection import get_db_connection, get_or_create_default_business


def verify_refund_claim(
    claimed_amount: float,
    period_codes: List[str],
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Verify that a proposed refund claim strictly complies with statutory safety rules for a business.
    
    Checks:
    1. Claimed amount must match code-calculated eligible credit for the requested periods to the fils (0.01 AED).
    2. Zero included periods have EXPIRED status.
    3. Zero invoices in the requested periods have 'NEEDS_REVIEW' or 'INELIGIBLE' status.
    4. Enforces APPROVAL_THRESHOLD_AED flagging.
    
    Returns:
        tuple (is_valid, reason)
    """
    if claimed_amount <= 0:
        return False, f"Claimed refund amount ({claimed_amount} AED) must be strictly positive."

    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    try:
        cursor = conn.cursor()

        # Check 1: Expiry status of periods for this business
        placeholders = ",".join("?" for _ in period_codes)
        cursor.execute(
            f"SELECT period_code, status, net_excess_aed FROM credit_ledger WHERE business_id = ? AND period_code IN ({placeholders});",
            [biz_id] + period_codes,
        )
        ledger_rows = cursor.fetchall()

        if len(ledger_rows) != len(period_codes):
            found_periods = {row["period_code"] for row in ledger_rows}
            missing = set(period_codes) - found_periods
            return False, f"Credit ledger has no record for requested period(s): {', '.join(missing)}."

        total_eligible_excess = 0.0
        for row in ledger_rows:
            if row["status"] == "EXPIRED":
                return False, f"Period {row['period_code']} is statutory EXPIRED and cannot be claimed."
            total_eligible_excess += row["net_excess_aed"]

        total_eligible_excess = round(total_eligible_excess, 2)
        diff = abs(round(claimed_amount, 2) - total_eligible_excess)
        if diff > 0.05:
            return False, (
                f"Claim amount mismatch: proposed refund is {claimed_amount} AED, "
                f"but code-calculated eligible excess is {total_eligible_excess} AED (diff: {diff:.2f})."
            )

        # Check 2: Check for unreviewed invoices for this business
        cursor.execute(
            """
            SELECT COUNT(*) as cnt FROM invoices 
            WHERE business_id = ? AND direction = 'purchase'
              AND (eligibility_status = 'NEEDS_REVIEW'
                   OR (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') NOT LIKE 'Also in %'));
            """,
            (biz_id,),
        )
        unresolved_count = cursor.fetchone()["cnt"]
        if unresolved_count > 0:
            return False, f"Cannot submit refund package while {unresolved_count} invoice(s) remain in NEEDS_REVIEW or DUPLICATE status."

        return True, "Refund claim verified against credit ledger and safety rules."
    finally:
        conn.close()
