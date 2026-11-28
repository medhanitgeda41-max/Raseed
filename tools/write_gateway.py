"""The SINGLE transactional write gateway for all mutating operations.

Golden Rule:
- Multi-business isolation enforced on every mutating operation.
- Role-based authorization: Only OWNER or APPROVER can approve consequential actions.
- ACCOUNTANT cannot approve consequential tax actions.
- execute_approved_action() re-runs safety guards and guarantees atomic SQLite rollback.
"""
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from database.connection import get_db_connection, get_or_create_default_business
from guards.refund_guard import verify_refund_claim


LOW_RISK_ACTION_TYPES = {
    "REQUEST_DOCUMENT_FROM_USER",
    "RECORD_FTA_REFERENCE",
    "PREPARE_REFUND_PACKAGE",
}

CONSEQUENTIAL_ACTION_TYPES = {
    "SUBMIT_REFUND_APPLICATION",
    "SEND_EXTERNAL_EMAIL",
    "MARK_INVOICE_STATUS",
}

AUTHORIZED_APPROVER_ROLES = {"OWNER", "APPROVER"}


def stage_action(
    action_type: str,
    payload: Dict[str, Any],
    reason: str,
    source: str = "AGENT",
    business_id: Optional[int] = None,
    user_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[int, str]:
    """Stage an action for a specific business tenant."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    t0 = time.time()

    risk_level = "LOW" if action_type in LOW_RISK_ACTION_TYPES else "CONSEQUENTIAL"
    initial_status = "AUTO_EXECUTED" if risk_level == "LOW" else "PENDING_APPROVAL"

    try:
        with conn:
            cursor = conn.cursor()

            # Record in staged_actions
            cursor.execute(
                """
                INSERT INTO staged_actions (
                    business_id, staged_by_user_id, action_type, payload_json, rationale, risk_level, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    biz_id,
                    user_id,
                    action_type,
                    json.dumps(payload),
                    reason,
                    risk_level,
                    initial_status,
                ),
            )
            action_id = cursor.lastrowid

            # If auto-executable, run handler directly within this transaction
            if initial_status == "AUTO_EXECUTED":
                _apply_action_mutation(cursor, action_type, payload, biz_id)

            # Record in immutable audit log
            duration_ms = round((time.time() - t0) * 1000, 2)
            cursor.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json, execution_time_ms)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    biz_id,
                    user_id,
                    source,
                    f"ACTION_STAGED:{action_type}",
                    json.dumps({"action_id": action_id, "status": initial_status, "payload": payload}),
                    duration_ms,
                ),
            )

        return action_id, initial_status
    finally:
        conn.close()


def execute_approved_action(
    action_id: int,
    approver_note: str = "",
    approver_role: str = "APPROVER",
    approver_user_id: Optional[int] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Execute a staged action after human approval.
    
    Enforces Role-Based Access Control (RBAC):
    - ACCOUNTANT role cannot approve. Only OWNER or APPROVER is authorized.
    Re-runs safety guards before committing.
    Rolls back transaction on any error.
    """
    # 1. RBAC Check
    if approver_role not in AUTHORIZED_APPROVER_ROLES:
        return False, f"Permission denied: Users with role '{approver_role}' are not authorized to approve tax actions."

    conn = get_db_connection(db_path=db_path, readonly=False)
    t0 = time.time()

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM staged_actions WHERE id = ?;", (action_id,))
        action = cursor.fetchone()

        if not action:
            return False, f"Staged action #{action_id} not found."

        act_biz_id = action["business_id"]
        if business_id is not None and act_biz_id != business_id:
            return False, f"Security violation: Action #{action_id} does not belong to business #{business_id}."

        if action["status"] != "PENDING_APPROVAL":
            return False, f"Action #{action_id} is in status '{action['status']}', cannot approve."

        action_type = action["action_type"]
        payload = json.loads(action["payload_json"])

        # Re-run safety guards for consequential actions
        if action_type == "SUBMIT_REFUND_APPLICATION":
            claimed_amount = float(payload.get("claimed_amount", 0.0))
            period_codes = payload.get("period_codes", [])
            valid, guard_reason = verify_refund_claim(claimed_amount, period_codes, business_id=act_biz_id, db_path=db_path)
            if not valid:
                return False, f"Guard verification failed prior to execution: {guard_reason}"

        # Atomic transaction execution with rollback protection
        with conn:
            _apply_action_mutation(cursor, action_type, payload, act_biz_id)

            cursor.execute(
                """
                UPDATE staged_actions
                SET status = 'APPROVED', approver_note = ?, decided_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (approver_note, action_id),
            )

            # Emit audit log
            duration_ms = round((time.time() - t0) * 1000, 2)
            cursor.execute(
                """
                INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json, execution_time_ms)
                VALUES (?, ?, 'USER', ?, ?, ?);
                """,
                (
                    act_biz_id,
                    approver_user_id,
                    f"ACTION_APPROVED:{action_type}",
                    json.dumps({"action_id": action_id, "note": approver_note, "approver_role": approver_role}),
                    duration_ms,
                ),
            )

        return True, f"Action #{action_id} ({action_type}) successfully executed and committed."

    except Exception as e:
        conn.rollback()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO audit_events (business_id, user_id, actor, event_type, details_json)
                    VALUES (?, ?, 'SYSTEM', 'ACTION_EXECUTION_FAILED', ?);
                    """,
                    (business_id, approver_user_id, json.dumps({"action_id": action_id, "error": str(e)})),
                )
        except Exception:
            pass
        return False, f"Execution aborted and rolled back due to error: {str(e)}"
    finally:
        conn.close()


def reject_staged_action(
    action_id: int,
    rejection_note: str = "",
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Mark a staged action as rejected. Leaves all financial records untouched."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            cursor = conn.cursor()
            if business_id:
                cursor.execute(
                    """
                    UPDATE staged_actions
                    SET status = 'REJECTED', approver_note = ?, decided_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND business_id = ? AND status = 'PENDING_APPROVAL';
                    """,
                    (rejection_note, action_id, business_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE staged_actions
                    SET status = 'REJECTED', approver_note = ?, decided_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'PENDING_APPROVAL';
                    """,
                    (rejection_note, action_id),
                )

            if cursor.rowcount == 0:
                return False, f"Action #{action_id} could not be rejected (not found or already decided)."

            cursor.execute(
                """
                INSERT INTO audit_events (business_id, actor, event_type, details_json)
                VALUES (?, 'USER', 'ACTION_REJECTED', ?);
                """,
                (business_id, json.dumps({"action_id": action_id, "note": rejection_note})),
            )
        return True, f"Action #{action_id} was rejected. No changes were made."
    finally:
        conn.close()


def _apply_action_mutation(cursor, action_type: str, payload: Dict[str, Any], business_id: int) -> None:
    """Internal mutating SQL statements executed inside the active transaction."""
    if action_type == "REQUEST_DOCUMENT_FROM_USER":
        cursor.execute(
            """
            INSERT INTO document_requests (business_id, period_code, invoice_reference, supplier_name, amount_aed, reason)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                business_id,
                payload.get("period_code", "GENERAL"),
                payload.get("invoice_reference"),
                payload.get("supplier_name"),
                payload.get("amount_aed"),
                payload.get("reason", "Verification required"),
            ),
        )

    elif action_type == "MARK_INVOICE_STATUS":
        cursor.execute(
            """
            UPDATE invoices SET eligibility_status = ?, eligibility_reason = ? 
            WHERE id = ? AND business_id = ?;
            """,
            (payload["status"], payload.get("reason"), payload["invoice_id"], business_id),
        )

    elif action_type == "RECORD_FTA_REFERENCE":
        cursor.execute(
            """
            UPDATE refund_records 
            SET emartax_application_ref = ?, fta_status = 'SUBMITTED', submission_date = CURRENT_DATE
            WHERE id = ? AND business_id = ?;
            """,
            (payload["emartax_reference"], payload["refund_record_id"], business_id),
        )

    elif action_type == "SUBMIT_REFUND_APPLICATION":
        cursor.execute(
            """
            INSERT INTO refund_records (business_id, period_codes, total_refund_claimed_aed, fta_status)
            VALUES (?, ?, ?, 'SUBMITTED');
            """,
            (business_id, json.dumps(payload["period_codes"]), payload["claimed_amount"]),
        )
        # Mark credit ledger rows as CLAIMED for this business
        for p in payload["period_codes"]:
            cursor.execute(
                "UPDATE credit_ledger SET status = 'CLAIMED', claimed_refund_amount_aed = net_excess_aed WHERE business_id = ? AND period_code = ?;",
                (business_id, p),
            )


def save_business_profile(
    business_id: int,
    legal_name: str,
    trn: str,
    vat_registration_date: str = "2020-01-01",
    tax_period_type: str = "quarterly",
    first_tax_period_start: str = "2020-01-01",
    business_activity: str = "General Trading",
    emirate: str = "Dubai",
    makes_exempt_supplies: int = 0,
    apportionment_percentage: float = 100.0,
    signatory_name: str = "Business Owner",
    signatory_email: str = "owner@business.ae",
    masked_iban: str = "AE23****************1234",
    db_path: Optional[Path] = None,
) -> bool:
    """Save or update the single business profile for the owner."""
    from guards.privacy import mask_iban as fn_mask_iban
    clean_trn = trn.strip()
    clean_iban = fn_mask_iban(masked_iban) if not masked_iban.startswith("AE") else masked_iban
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            # 1. Update or insert into businesses table
            cur = conn.cursor()
            cur.execute("SELECT id FROM businesses WHERE id = ?;", (business_id,))
            if cur.fetchone():
                conn.execute(
                    "UPDATE businesses SET name = ?, trn = ? WHERE id = ?;",
                    (legal_name.strip(), clean_trn, business_id),
                )
            else:
                conn.execute(
                    "INSERT INTO businesses (id, name, trn) VALUES (?, ?, ?);",
                    (business_id, legal_name.strip(), clean_trn),
                )
            # 2. Upsert business_profile
            conn.execute(
                """
                INSERT INTO business_profile (
                    business_id, legal_name, trn, vat_registration_date,
                    tax_period_type, first_tax_period_start, business_activity,
                    emirate, makes_exempt_supplies, apportionment_percentage,
                    signatory_name, signatory_email, masked_iban
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_id) DO UPDATE SET
                    legal_name = excluded.legal_name,
                    trn = excluded.trn,
                    vat_registration_date = excluded.vat_registration_date,
                    tax_period_type = excluded.tax_period_type,
                    first_tax_period_start = excluded.first_tax_period_start,
                    business_activity = excluded.business_activity,
                    emirate = excluded.emirate,
                    makes_exempt_supplies = excluded.makes_exempt_supplies,
                    apportionment_percentage = excluded.apportionment_percentage,
                    signatory_name = excluded.signatory_name,
                    signatory_email = excluded.signatory_email,
                    masked_iban = excluded.masked_iban,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    business_id,
                    legal_name.strip(),
                    clean_trn,
                    str(vat_registration_date),
                    tax_period_type,
                    str(first_tax_period_start),
                    business_activity,
                    emirate,
                    int(makes_exempt_supplies),
                    float(apportionment_percentage),
                    signatory_name,
                    signatory_email,
                    clean_iban,
                ),
            )
        return True
    finally:
        conn.close()


def refresh_pending_refund_actions(
    business_id: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Keep refund requests that are still waiting for approval in step with the books.

    Called after every upload or correction: each PENDING_APPROVAL refund is
    recalculated from the credit ledger (code, not AI). Approved or rejected
    actions are never changed. Returns the list of changes made.
    """
    from tools.compute_tools import compute_refund_claim

    conn = get_db_connection(db_path=db_path, readonly=False)
    changes: List[Dict[str, Any]] = []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, payload_json FROM staged_actions WHERE business_id = ? AND status = 'PENDING_APPROVAL' "
            "AND action_type IN ('SUBMIT_REFUND_APPLICATION', 'PREPARE_REFUND_PACKAGE');",
            (business_id,),
        )
        pending = [dict(r) for r in cur.fetchall()]
        if not pending:
            return []
        claim = compute_refund_claim(business_id=business_id, db_path=db_path)
        with conn:
            for act in pending:
                try:
                    payload = json.loads(act["payload_json"] or "{}")
                except Exception:
                    payload = {}
                old_amt = round(float(payload.get("claimed_amount", 0.0) or 0.0), 2)
                old_periods = list(payload.get("period_codes", []) or [])
                new_amt, new_periods = claim["claimed_amount"], claim["period_codes"]
                if abs(old_amt - new_amt) < 0.01 and sorted(old_periods) == sorted(new_periods):
                    continue
                payload["claimed_amount"] = new_amt
                payload["period_codes"] = new_periods
                conn.execute("UPDATE staged_actions SET payload_json = ? WHERE id = ?;", (json.dumps(payload), act["id"]))
                conn.execute(
                    "INSERT INTO audit_events (business_id, actor, event_type, details_json) VALUES (?, 'SYSTEM', ?, ?);",
                    (business_id, "REFUND_RECALCULATED", json.dumps({
                        "action_id": act["id"], "old_amount": old_amt, "new_amount": new_amt,
                        "old_periods": old_periods, "new_periods": new_periods,
                    })),
                )
                changes.append({"action_id": act["id"], "old_amount": old_amt, "new_amount": new_amt,
                                "old_periods": old_periods, "new_periods": new_periods})
        return changes
    finally:
        conn.close()
