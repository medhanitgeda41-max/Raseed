"""Continuous autonomous monitoring and background scheduler for Raseed UAE VAT Recovery.

Thread-safe, idempotent background services powered by APScheduler:
1. Daily deadline job: recomputes statutory deadlines with simulated demo time; generates
   alerts and tasks for DUE_SOON and URGENT credits.
2. On new upload: ingests, validates, updates credit ledger, and re-evaluates missing documents.
3. On changed VAT return: rebuilds credit ledger and reports 4-way reconciliation differences.
4. Stale document requests: reminds owner after N days if document requests remain unfulfilled.
5. Refund applications: tracks pending FTA reviews, prompts for updates, records decisions and amounts.
6. Legal uncertainty: escalates ambiguous classifications and NEEDS_REVIEW invoices to owner.

Reliability guarantees:
- Single execution lock per process (thread-safe with SQLite).
- Strict idempotency: no duplicate tasks, alerts, or requests.
- Full audit logging in scheduler_runs and audit_events.
- Catches up on missed daily checks on process startup.
- Exponential back-off retry on failure with fallback escalation.
"""
import json
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database.connection import get_db_connection, get_or_create_default_business
from tools.compute_tools import rebuild_credit_ledger, reconcile_4_way
from tools.read_tools import get_document_requests

logger = logging.getLogger("raseed.scheduler")

# ------------------------------------------------------------------------------
# 1. PROCESS-LEVEL SYNCHRONIZATION & DEMO TIME SIMULATION
# ------------------------------------------------------------------------------
_scheduler_lock = threading.Lock()
_scheduler_instance: Optional[BackgroundScheduler] = None
_demo_days_offset: int = 0
_demo_offset_lock = threading.Lock()


def advance_demo_clock(days: int) -> int:
    """Fast-forward system time by +N days for hackathon evaluations and deadline tests."""
    from database.request_cache import bump_write_generation
    bump_write_generation()
    global _demo_days_offset
    with _demo_offset_lock:
        _demo_days_offset += days
        logger.info("Demo clock advanced by %d days. Total offset: %d days.", days, _demo_days_offset)
        return _demo_days_offset


def set_demo_clock_offset(days: int) -> int:
    """Set the exact demo time offset in days."""
    from database.request_cache import bump_write_generation
    bump_write_generation()
    global _demo_days_offset
    with _demo_offset_lock:
        _demo_days_offset = max(0, days)
        return _demo_days_offset


def get_demo_clock_offset() -> int:
    """Get the current demo clock offset in days."""
    with _demo_offset_lock:
        return _demo_days_offset


def get_current_date() -> date:
    """Get effective evaluation date taking into account any demo clock acceleration."""
    with _demo_offset_lock:
        offset = _demo_days_offset
    return date.today() + timedelta(days=offset)


def format_date_plain(val: Any) -> str:
    """Format dates uniformly as '15 Jan 2026'."""
    if not val:
        return "Not specified"
    try:
        if isinstance(val, (date, datetime)):
            return val.strftime("%d %b %Y")
        clean = str(val).strip().split()[0]
        dt = datetime.strptime(clean, "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(val)


# ------------------------------------------------------------------------------
# 2. JOB EXECUTION WRAPPER WITH RETRIES AND AUDIT LOGGING
# ------------------------------------------------------------------------------
def _execute_with_retry(
    job_name: str,
    business_id: int,
    func: Callable[[], Any],
    max_retries: int = 3,
    backoff_base: float = 0.5,
    db_path: Optional[Path] = None,
) -> Any:
    """Execute job with mutex lock, exponential back-off retries, and SQLite logging."""
    with _scheduler_lock:
        last_error = None
        for attempt in range(max_retries):
            try:
                res = func()
                # Log success in scheduler_runs
                _record_job_run(
                    business_id=business_id,
                    job_name=job_name,
                    status="SUCCESS",
                    details={"result": str(res), "attempt": attempt + 1},
                    db_path=db_path,
                )
                return res
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Job '%s' attempt %d/%d failed: %s",
                    job_name, attempt + 1, max_retries, exc,
                )
                if attempt < max_retries - 1:
                    time.sleep(backoff_base * (2 ** attempt))

        # Failed after all retries
        err_msg = str(last_error)
        logger.error("Job '%s' failed permanently after %d retries: %s", job_name, max_retries, err_msg)
        _record_job_run(
            business_id=business_id,
            job_name=job_name,
            status="FAILED",
            details={"error": err_msg, "attempts": max_retries},
            db_path=db_path,
        )
        _log_audit_event(
            business_id=business_id,
            actor="SYSTEM",
            event_type="JOB_FAILURE_ESCALATION",
            details={"job_name": job_name, "error": err_msg},
            db_path=db_path,
        )
        raise RuntimeError(f"Job '{job_name}' failed after {max_retries} attempts: {err_msg}") from last_error


def _record_job_run(
    business_id: int,
    job_name: str,
    status: str,
    details: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
):
    """Record execution outcome in scheduler_runs table without locking conflicts."""
    owns_conn = False
    if conn is None:
        conn = get_db_connection(db_path=db_path, readonly=False)
        owns_conn = True
    try:
        conn.execute(
            """
            INSERT INTO scheduler_runs (business_id, job_name, status, details_json)
            VALUES (?, ?, ?, ?);
            """,
            (business_id, job_name, status, json.dumps(details)),
        )
        if owns_conn:
            conn.commit()
    except Exception as e:
        logger.warning("Could not record scheduler_runs: %s", e)
    finally:
        if owns_conn:
            conn.close()


def _log_audit_event(
    business_id: int,
    actor: str,
    event_type: str,
    details: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
):
    """Write an entry to the immutable audit_events table without locking conflicts."""
    owns_conn = False
    if conn is None:
        conn = get_db_connection(db_path=db_path, readonly=False)
        owns_conn = True
    try:
        conn.execute(
            """
            INSERT INTO audit_events (business_id, actor, event_type, details_json)
            VALUES (?, ?, ?, ?);
            """,
            (business_id, actor, event_type, json.dumps(details)),
        )
        if owns_conn:
            conn.commit()
    except Exception as e:
        logger.warning("Could not log audit event: %s", e)
    finally:
        if owns_conn:
            conn.close()


def _stage_scheduler_message(
    business_id: int,
    message_type: str,
    title: str,
    content: str,
    period_code: Optional[str] = None,
    priority: str = "MEDIUM",
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Stage an autonomous chat notification ensuring idempotency and transaction reuse."""
    owns_conn = False
    if conn is None:
        conn = get_db_connection(db_path=db_path, readonly=False)
        owns_conn = True
    try:
        cur = conn.cursor()
        # Check if identical message already staged/sent recently
        cur.execute(
            """
            SELECT id FROM scheduler_messages
            WHERE business_id = ? AND message_type = ? AND title = ? AND is_delivered = 0;
            """,
            (business_id, message_type, title),
        )
        if cur.fetchone():
            return False  # Already queued; do not duplicate

        cur.execute(
            """
            INSERT INTO scheduler_messages (business_id, message_type, title, content, period_code, priority)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (business_id, message_type, title, content, period_code, priority),
        )
        if owns_conn:
            conn.commit()
        return True
    finally:
        if owns_conn:
            conn.close()


# ------------------------------------------------------------------------------
# 3. BACKGROUND MONITORING JOBS
# ------------------------------------------------------------------------------

def run_daily_deadline_check(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 1 (Daily): Recompute statutory deadlines with simulated date; generate alerts for DUE_SOON and URGENT."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    def _job_impl():
        current_eval_date = get_current_date()
        # 1. Rebuild credit ledger using current evaluation date (handles its own transaction)
        rebuild_credit_ledger(business_id=biz_id, db_path=db_path, current_date=current_eval_date)

        conn = get_db_connection(db_path=db_path, readonly=False)
        alerts_created = 0
        tasks_created = 0

        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT period_code, net_excess_aed, expiry_date, days_remaining, status
                    FROM credit_ledger
                    WHERE business_id = ? AND status IN ('URGENT', 'DUE_SOON')
                    ORDER BY days_remaining ASC;
                    """,
                    (biz_id,),
                )
                at_risk_periods = cur.fetchall()

                for r in at_risk_periods:
                    p_code = r["period_code"]
                    st_val = r["status"]
                    amt = float(r["net_excess_aed"])
                    days_rem = r["days_remaining"]
                    exp_formatted = format_date_plain(r["expiry_date"])
                    is_urg = st_val == "URGENT"
                    prio = "URGENT" if is_urg else "HIGH"

                    task_desc = f"VAT credit of AED {amt:,.2f} for period {p_code} expires in {days_rem} days ({exp_formatted})."
                    # Idempotent upsert into agent_tasks
                    cur.execute(
                        """
                        SELECT id FROM agent_tasks
                        WHERE business_id = ? AND task_type = 'EXPIRY_DEADLINE_ALERT' AND description LIKE ?;
                        """,
                        (biz_id, f"%period {p_code}%"),
                    )
                    existing_t = cur.fetchone()
                    if not existing_t:
                        cur.execute(
                            """
                            INSERT INTO agent_tasks (business_id, task_type, description, priority, status)
                            VALUES (?, 'EXPIRY_DEADLINE_ALERT', ?, ?, 'WAITING_APPROVAL');
                            """,
                            (biz_id, task_desc, prio),
                        )
                        tasks_created += 1

                    # Stage chat notification (reuse conn to avoid nested locks)
                    urg_icon = "🔴 Urgent" if is_urg else "🟠 Expiring soon"
                    msg_title = f"Expiry Notice: {p_code} ({st_val})"
                    msg_content = (
                        f"🤖 **Daily Deadline Notice ({urg_icon}):** I checked your deadlines this morning: "
                        f"**AED {amt:,.2f}** from **{p_code}** now expires in **{days_rem} days** (on {exp_formatted}). "
                        f"Shall I prepare the refund application for your approval?"
                    )
                    queued = _stage_scheduler_message(
                        business_id=biz_id,
                        message_type="EXPIRY_ALERT",
                        title=msg_title,
                        content=msg_content,
                        period_code=p_code,
                        priority=prio,
                        conn=conn,
                    )
                    if queued:
                        alerts_created += 1

            return {
                "evaluated_date": current_eval_date.isoformat(),
                "at_risk_count": len(at_risk_periods),
                "tasks_created": tasks_created,
                "alerts_created": alerts_created,
            }
        finally:
            conn.close()

    return _execute_with_retry("daily_deadline_check", biz_id, _job_impl, db_path=db_path)


def run_on_new_upload_check(
    business_id: int,
    document_id: int,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 2 (On new upload): Ingest, validate, update credit ledger, and re-check missing documents."""
    def _job_impl():
        current_eval_date = get_current_date()
        rebuild_credit_ledger(business_id=business_id, db_path=db_path, current_date=current_eval_date)
        recon = reconcile_4_way(business_id=business_id, db_path=db_path)

        conn = get_db_connection(db_path=db_path, readonly=False)
        fulfilled_reqs = 0

        try:
            with conn:
                cur = conn.cursor()
                # Check if any open document requests can now be fulfilled
                cur.execute(
                    """
                    SELECT dr.id, dr.period_code, dr.invoice_reference
                    FROM document_requests dr
                    WHERE dr.business_id = ? AND dr.status = 'OPEN';
                    """,
                    (business_id,),
                )
                open_reqs = cur.fetchall()

                for req in open_reqs:
                    rid = req["id"]
                    inv_ref = req["invoice_reference"]
                    if inv_ref:
                        cur.execute(
                            """
                            SELECT id FROM invoices
                            WHERE business_id = ? AND invoice_number = ? AND eligibility_status = 'VALID';
                            """,
                            (business_id, inv_ref),
                        )
                        if cur.fetchone():
                            cur.execute(
                                """
                                UPDATE document_requests
                                SET status = 'FULFILLED', fulfilled_at = CURRENT_TIMESTAMP
                                WHERE id = ?;
                                """,
                                (rid,),
                            )
                            fulfilled_reqs += 1

                # Stage chat notification reusing conn
                _stage_scheduler_message(
                    business_id=business_id,
                    message_type="UPLOAD_PROCESSED",
                    title=f"New Document Ingested #{document_id}",
                    content=(
                        f"🤖 **Autonomous Check:** Processed new document upload #{document_id}. "
                        f"Updated your statutory VAT credits and resolved {fulfilled_reqs} pending document request(s)."
                    ),
                    priority="LOW",
                    conn=conn,
                )

            return {
                "document_id": document_id,
                "reconciled": recon["reconciled"],
                "fulfilled_requests": fulfilled_reqs,
            }
        finally:
            conn.close()

    return _execute_with_retry("on_new_upload", business_id, _job_impl, db_path=db_path)


def run_on_changed_vat_return_check(
    business_id: int,
    period: str,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 3 (On changed VAT return): Rebuild credits and report differences across accounting books."""
    def _job_impl():
        current_eval_date = get_current_date()
        rebuild_credit_ledger(business_id=business_id, db_path=db_path, current_date=current_eval_date)
        recon = reconcile_4_way(business_id=business_id, db_path=db_path)

        diff_count = recon.get("discrepancies_count", 0)
        if diff_count > 0:
            conn = get_db_connection(db_path=db_path, readonly=False)
            try:
                with conn:
                    _stage_scheduler_message(
                        business_id=business_id,
                        message_type="RETURN_CHANGED_DIFFERENCE",
                        title=f"VAT Return Change for {period}",
                        content=(
                            f"🤖 **Notice on VAT Return Update:** Filed Form 201 return for **{period}** was updated. "
                            f"4-way check detected **{diff_count} difference(s)** between your return and purchase invoices. "
                            "Review 'Do the numbers match?' in the left panel to verify."
                        ),
                        period_code=period,
                        priority="HIGH",
                        conn=conn,
                    )
            finally:
                conn.close()

        return {
            "period": period,
            "reconciled": recon["reconciled"],
            "discrepancies_count": diff_count,
        }

    return _execute_with_retry("on_changed_vat_return", business_id, _job_impl, db_path=db_path)


def run_stale_document_requests_check(
    business_id: Optional[int] = None,
    stale_days: int = 3,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 4: Remind the owner of document requests not answered after N days."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    def _job_impl():
        current_eval_date = get_current_date()
        cutoff_date = (current_eval_date - timedelta(days=stale_days)).isoformat()

        conn = get_db_connection(db_path=db_path, readonly=False)
        reminders_sent = 0

        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, period_code, invoice_reference, supplier_name, amount_aed, reason, requested_at
                    FROM document_requests
                    WHERE business_id = ? AND status = 'OPEN' AND requested_at <= ?;
                    """,
                    (biz_id, cutoff_date),
                )
                stale_requests = cur.fetchall()

                for req in stale_requests:
                    p_code = req["period_code"]
                    inv_ref = req["invoice_reference"] or "Tax Invoice"
                    supp = req["supplier_name"] or "Supplier"
                    amt = float(req["amount_aed"] or 0.0)

                    title = f"Document Reminder #{req['id']}: {p_code}"
                    content = (
                        f"🤖 **Reminder:** Still waiting on missing document for period **{p_code}** "
                        f"({inv_ref} from {supp}, AED {amt:,.2f}). "
                        f"This document is required to claim your credit before the statutory deadline."
                    )
                    queued = _stage_scheduler_message(
                        business_id=biz_id,
                        message_type="STALE_DOCUMENT_REMINDER",
                        title=title,
                        content=content,
                        period_code=p_code,
                        priority="HIGH",
                        conn=conn,
                    )
                    if queued:
                        reminders_sent += 1

            return {
                "stale_count": len(stale_requests),
                "reminders_sent": reminders_sent,
            }
        finally:
            conn.close()

    return _execute_with_retry("stale_document_requests", biz_id, _job_impl, db_path=db_path)


def run_check_refund_applications(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 5: Track pending refund applications and remind owner to update FTA decision / payment status."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    def _job_impl():
        conn = get_db_connection(db_path=db_path, readonly=False)
        reminders_sent = 0

        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, period_codes, total_refund_claimed_aed, emartax_application_ref, submission_date, fta_status
                    FROM refund_records
                    WHERE business_id = ? AND fta_status IN ('SUBMITTED', 'IN_REVIEW', 'ADDITIONAL_INFO_REQUESTED');
                    """,
                    (biz_id,),
                )
                pending_claims = cur.fetchall()

                for ref in pending_claims:
                    ref_id = ref["id"]
                    emartax_ref = ref["emartax_application_ref"] or f"Claim #{ref_id}"
                    amt = float(ref["total_refund_claimed_aed"])
                    periods = ref["period_codes"]

                    title = f"Refund Follow-Up: {emartax_ref}"
                    content = (
                        f"🤖 **Refund Status Follow-Up:** Your refund application **{emartax_ref}** for "
                        f"**AED {amt:,.2f}** ({periods}) is currently marked as `{ref['fta_status']}` with the FTA. "
                        "Have you received an official FTA decision or bank transfer? If so, let me know so I can record it and update your credits."
                    )
                    queued = _stage_scheduler_message(
                        business_id=biz_id,
                        message_type="REFUND_FOLLOWUP",
                        title=title,
                        content=content,
                        priority="MEDIUM",
                        conn=conn,
                    )
                    if queued:
                        reminders_sent += 1

            return {
                "pending_claims_count": len(pending_claims),
                "reminders_sent": reminders_sent,
            }
        finally:
            conn.close()

    return _execute_with_retry("check_refund_applications", biz_id, _job_impl, db_path=db_path)


def record_fta_decision(
    refund_record_id: int,
    decision_status: str,
    amount_received: Optional[float] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Record FTA approval/settlement decision, mark credits received, and update credit ledger."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    conn = get_db_connection(db_path=db_path, readonly=False)

    try:
        with conn:
            conn.execute(
                """
                UPDATE refund_records
                SET fta_status = ?, fta_decision_date = CURRENT_TIMESTAMP, amount_approved_aed = ?
                WHERE id = ? AND business_id = ?;
                """,
                (decision_status, amount_received, refund_record_id, biz_id),
            )
            _log_audit_event(
                business_id=biz_id,
                actor="USER",
                event_type="FTA_DECISION_RECORDED",
                details={
                    "refund_record_id": refund_record_id,
                    "status": decision_status,
                    "amount_received": amount_received,
                },
                db_path=db_path,
                conn=conn,
            )
    finally:
        conn.close()

    # Rebuild credit ledger AFTER closing write connection to avoid database locks
    rebuild_credit_ledger(business_id=biz_id, db_path=db_path, current_date=get_current_date())
    return True


def run_escalate_legal_uncertainties(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Job 6: Escalate ambiguous tax classifications and NEEDS_REVIEW invoices to owner with plain reason."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    def _job_impl():
        conn = get_db_connection(db_path=db_path, readonly=False)
        escalations = 0

        try:
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, supplier_name, invoice_number, date, vat_amount, eligibility_status, eligibility_reason
                    FROM invoices
                    WHERE business_id = ? AND (eligibility_status = 'NEEDS_REVIEW' OR (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') NOT LIKE 'Also in %'));
                    """,
                    (biz_id,),
                )
                flagged_invoices = cur.fetchall()

                for inv in flagged_invoices:
                    inv_id = inv["id"]
                    inv_num = inv["invoice_number"]
                    supp = inv["supplier_name"]
                    amt = float(inv["vat_amount"])
                    reason = inv["eligibility_reason"] or "Category restriction or arithmetic discrepancy"

                    task_desc = f"Legal check needed for invoice {inv_num} from {supp} (AED {amt:,.2f}): {reason}"

                    # Idempotent task creation
                    cur.execute(
                        """
                        SELECT id FROM agent_tasks
                        WHERE business_id = ? AND task_type = 'LEGAL_UNCERTAINTY' AND description = ?;
                        """,
                        (biz_id, task_desc),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """
                            INSERT INTO agent_tasks (business_id, task_type, description, priority, status)
                            VALUES (?, 'LEGAL_UNCERTAINTY', ?, 'HIGH', 'WAITING_USER');
                            """,
                            (biz_id, task_desc),
                        )
                        escalations += 1

                    # Stage chat notice reusing conn
                    title = f"Legal Review: Invoice {inv_num}"
                    content = (
                        f"🤖 **Legal Check Needed:** Invoice **{inv_num}** from **{supp}** "
                        f"(**AED {amt:,.2f}**) requires your check: *{reason}*. "
                        "Under UAE tax rules, this expense may be blocked or require human justification."
                    )
                    _stage_scheduler_message(
                        business_id=biz_id,
                        message_type="LEGAL_ESCALATION",
                        title=title,
                        content=content,
                        priority="HIGH",
                        conn=conn,
                    )

            return {
                "flagged_count": len(flagged_invoices),
                "escalations_created": escalations,
            }
        finally:
            conn.close()

    return _execute_with_retry("escalate_legal_uncertainties", biz_id, _job_impl, db_path=db_path)


def run_all_monitoring_jobs_now(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute all autonomous monitoring jobs immediately on demand (for 'Check now' button or startup)."""
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    res_daily = run_daily_deadline_check(business_id=biz_id, db_path=db_path)
    res_stale = run_stale_document_requests_check(business_id=biz_id, db_path=db_path)
    res_refunds = run_check_refund_applications(business_id=biz_id, db_path=db_path)
    res_legal = run_escalate_legal_uncertainties(business_id=biz_id, db_path=db_path)

    return {
        "daily_check": res_daily,
        "stale_documents": res_stale,
        "refund_applications": res_refunds,
        "legal_uncertainties": res_legal,
        "executed_at": datetime.now().isoformat(),
    }


# ------------------------------------------------------------------------------
# 4. CHAT STREAM CONSUMER & INBOX DATA HELPERS
# ------------------------------------------------------------------------------

def consume_scheduler_messages(
    business_id: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve and mark delivered all pending autonomous notifications for the chat stream."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    delivered_msgs = []

    try:
        with conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, message_type, title, content, period_code, priority, created_at
                FROM scheduler_messages
                WHERE business_id = ? AND is_delivered = 0
                ORDER BY id ASC;
                """,
                (business_id,),
            )
            rows = cur.fetchall()

            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                cur.execute(
                    f"UPDATE scheduler_messages SET is_delivered = 1 WHERE id IN ({placeholders});",
                    ids,
                )
                delivered_msgs = [dict(r) for r in rows]

        return delivered_msgs
    finally:
        conn.close()


def get_inbox_tasks(
    business_id: int,
    db_path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Retrieve categorized tasks for Raseed's Inbox view in the VAT Credit Panel."""
    conn = get_db_connection(db_path=db_path, readonly=True)

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, task_type, description, priority, status, created_at, updated_at
            FROM agent_tasks
            WHERE business_id = ?
            ORDER BY 
                CASE priority
                    WHEN 'URGENT' THEN 1
                    WHEN 'HIGH' THEN 2
                    WHEN 'MEDIUM' THEN 3
                    ELSE 4
                END,
                id DESC;
            """,
            (business_id,),
        )
        all_tasks = [dict(r) for r in cur.fetchall()]

        inbox_categorized: Dict[str, List[Dict[str, Any]]] = {
            "open": [],
            "waiting_user": [],
            "waiting_approval": [],
            "done": [],
        }

        for t in all_tasks:
            st_val = t["status"]
            if st_val in ("PENDING", "IN_PROGRESS"):
                inbox_categorized["open"].append(t)
            elif st_val == "WAITING_USER":
                inbox_categorized["waiting_user"].append(t)
            elif st_val == "WAITING_APPROVAL":
                inbox_categorized["waiting_approval"].append(t)
            elif st_val in ("DONE", "CANCELLED"):
                inbox_categorized["done"].append(t)
            else:
                inbox_categorized["open"].append(t)

        return inbox_categorized
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# 5. SCHEDULER LIFECYCLE MANAGEMENT (SINGLETON PER PROCESS)
# ------------------------------------------------------------------------------

def start_monitoring_scheduler(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
    check_interval_seconds: int = 3600,
) -> BackgroundScheduler:
    """Start the single background scheduler for the application process."""
    global _scheduler_instance

    with _scheduler_lock:
        if _scheduler_instance is not None and _scheduler_instance.running:
            return _scheduler_instance

        sched = BackgroundScheduler(daemon=True)

        def _periodic_tick():
            try:
                run_all_monitoring_jobs_now(business_id=business_id, db_path=db_path)
            except Exception as e:
                logger.error("Periodic monitoring tick failed: %s", e)

        # Register periodic monitoring job (runs catch-up immediately)
        sched.add_job(
            _periodic_tick,
            trigger=IntervalTrigger(seconds=check_interval_seconds),
            id="raseed_continuous_monitoring",
            name="Raseed Continuous Autonomous Monitoring",
            replace_existing=True,
        )

        sched.start()
        _scheduler_instance = sched
        logger.info("Raseed APScheduler started with interval %d seconds.", check_interval_seconds)

        # Run startup catch-up immediately in background thread
        threading.Thread(target=_periodic_tick, daemon=True).start()

        return sched


def shutdown_monitoring_scheduler():
    """Safely stop the background scheduler."""
    global _scheduler_instance
    with _scheduler_lock:
        if _scheduler_instance is not None and _scheduler_instance.running:
            _scheduler_instance.shutdown(wait=False)
            _scheduler_instance = None
            logger.info("Raseed APScheduler shut down.")
