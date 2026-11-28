"""Comprehensive test suite for Raseed Continuous Autonomous Monitoring & Background Scheduler.

Verifies:
1. Thread-safe process-level singleton lifecycle and locking.
2. Job 1 (Daily): Recomputes statutory deadlines with mocked/demo time; creates tasks and alerts for DUE_SOON and URGENT credits.
3. Job 2 (On new upload): Recomputes credit ledger and resolves unfulfilled document requests.
4. Job 3 (On changed VAT return): Rebuilds credit ledger and detects 4-way reconciliation differences.
5. Job 4 (Stale document requests): Sends reminders for requests > N days old without spamming.
6. Job 5 (Refund applications): Follows up on pending FTA claims; records official FTA decision and updates credits.
7. Job 6 (Legal uncertainty): Escalates ambiguous tax classifications and NEEDS_REVIEW invoices to owner.
8. Idempotency: Multiple executions create zero duplicate tasks, alerts, or requests.
9. Retry with back-off on temporary failure.
10. Inbox task categorization: open, waiting for you, waiting for your OK, done.
"""
from database.connection import get_db_connection  # engine-agnostic test connection
from datetime import date, timedelta
from pathlib import Path
import pytest
import sqlite3

from database.connection import init_db, get_db_connection, create_business
from tools.read_tools import get_document_requests
from scheduler.monitor import (
    start_monitoring_scheduler,
    shutdown_monitoring_scheduler,
    advance_demo_clock,
    set_demo_clock_offset,
    get_demo_clock_offset,
    get_current_date,
    run_daily_deadline_check,
    run_on_new_upload_check,
    run_on_changed_vat_return_check,
    run_stale_document_requests_check,
    run_check_refund_applications,
    run_escalate_legal_uncertainties,
    run_all_monitoring_jobs_now,
    record_fta_decision,
    consume_scheduler_messages,
    get_inbox_tasks,
    _execute_with_retry,
)
from tools.compute_tools import rebuild_credit_ledger


@pytest.fixture
def temp_db(tmp_path: Path):
    """Create a clean isolated SQLite database for scheduler tests."""
    db_file = tmp_path / "test_scheduler.db"
    init_db(db_path=db_file)
    set_demo_clock_offset(0)
    yield db_file
    set_demo_clock_offset(0)


@pytest.fixture
def seeded_biz(temp_db: Path) -> int:
    """Seed a test business with profile, invoices, returns, and credit ledger."""
    biz_id = create_business(name="Test Logistics FZE", trn="100999111222003", db_path=temp_db)
    conn = get_db_connection(db_path=temp_db, readonly=False)

    with conn:
        # Profile
        conn.execute(
            """
            INSERT INTO business_profile (
                business_id, legal_name, trn, vat_registration_date,
                tax_period_type, first_tax_period_start, business_activity,
                emirate, makes_exempt_supplies, signatory_name, signatory_email, masked_iban
            ) VALUES (?, 'Test Logistics FZE', '100999111222003', '2021-01-01', 'quarterly', '2021-01-01', 'Logistics', 'Dubai', 0, 'Sara', 'sara@test.ae', 'AEXX');
            """,
            (biz_id,),
        )
        # Filed VAT Return 2021-Q1
        conn.execute(
            """
            INSERT INTO vat_returns (
                business_id, period, period_start, period_end, output_tax, input_tax, net_payable_refundable
            ) VALUES (?, '2021-Q1', '2021-01-01', '2021-03-31', 1000.0, 7300.0, -6300.0);
            """,
            (biz_id,),
        )
        # Invoices for 2021-Q1
        conn.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn, invoice_number, date,
                taxable_amount, vat_amount, total, eligibility_status
            ) VALUES (?, 'purchase', 'Port Supplies LLC', '100555666777001', 'INV-2021-01', '2021-02-15', 146000.0, 7300.0, 153300.0, 'VALID');
            """,
            (biz_id,),
        )
    conn.close()

    # Rebuild ledger
    rebuild_credit_ledger(business_id=biz_id, db_path=temp_db)
    return biz_id


def test_singleton_scheduler_lifecycle():
    """Verify scheduler starts once as a process-level singleton and shuts down safely."""
    sched1 = start_monitoring_scheduler(check_interval_seconds=7200)
    assert sched1.running is True

    # Starting again returns the same running instance
    sched2 = start_monitoring_scheduler(check_interval_seconds=7200)
    assert sched1 is sched2

    shutdown_monitoring_scheduler()
    assert sched1.running is False


def test_demo_time_acceleration():
    """Verify demo clock moves forward and get_current_date reflects offsets accurately."""
    set_demo_clock_offset(0)
    today = date.today()
    assert get_current_date() == today

    advance_demo_clock(45)
    assert get_demo_clock_offset() == 45
    assert get_current_date() == today + timedelta(days=45)

    set_demo_clock_offset(90)
    assert get_current_date() == today + timedelta(days=90)
    set_demo_clock_offset(0)


def test_job1_daily_deadline_check_creates_tasks_and_alerts(temp_db: Path, seeded_biz: int):
    """Job 1: Daily deadline check detects DUE_SOON and URGENT credits and stages notifications."""
    # Fast forward demo clock so 2021-Q1 (which had standard expiry 2026-03-31, transitional 2026-12-31)
    # becomes DUE_SOON (<180 days)
    advance_demo_clock(30)
    res = run_daily_deadline_check(business_id=seeded_biz, db_path=temp_db)

    assert "evaluated_date" in res
    assert res["at_risk_count"] >= 1

    # Verify task created in agent_tasks
    inbox = get_inbox_tasks(business_id=seeded_biz, db_path=temp_db)
    all_pending = inbox["open"] + inbox["waiting_approval"]
    assert any("2021-Q1" in t["description"] for t in all_pending)

    # Verify notification staged in scheduler_messages
    msgs = consume_scheduler_messages(business_id=seeded_biz, db_path=temp_db)
    assert len(msgs) >= 1
    assert any("2021-Q1" in m["content"] for m in msgs)
    assert any("6,300" in m["content"] for m in msgs)

    # Calling consume again returns empty (already marked delivered)
    assert len(consume_scheduler_messages(business_id=seeded_biz, db_path=temp_db)) == 0


def test_job1_daily_deadline_check_is_idempotent(temp_db: Path, seeded_biz: int):
    """Verify repeated runs do NOT create duplicate tasks or duplicate unread messages."""
    run_daily_deadline_check(business_id=seeded_biz, db_path=temp_db)
    run_daily_deadline_check(business_id=seeded_biz, db_path=temp_db)
    run_daily_deadline_check(business_id=seeded_biz, db_path=temp_db)

    conn = get_db_connection(db_path=temp_db, readonly=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) as cnt FROM agent_tasks WHERE business_id = ? AND task_type = 'EXPIRY_DEADLINE_ALERT';",
        (seeded_biz,),
    )
    task_count = cur.fetchone()["cnt"]
    conn.close()

    # Must not duplicate
    assert task_count == 1


def test_job2_on_new_upload_resolves_open_document_requests(temp_db: Path, seeded_biz: int):
    """Job 2: New upload check fulfills pending document requests when matching invoice arrives."""
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        conn.execute(
            """
            INSERT INTO document_requests (business_id, period_code, invoice_reference, supplier_name, amount_aed, reason, status)
            VALUES (?, '2021-Q1', 'INV-NEEDED-99', 'Al Etihad Fuel', 500.0, 'Missing fuel invoice', 'OPEN');
            """,
            (seeded_biz,),
        )
    conn.close()

    # Upload matching valid invoice
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        conn.execute(
            """
            INSERT INTO invoices (business_id, direction, supplier_name, supplier_trn, invoice_number, date, taxable_amount, vat_amount, total, eligibility_status)
            VALUES (?, 'purchase', 'Al Etihad Fuel', '100444555666001', 'INV-NEEDED-99', '2021-02-10', 10000.0, 500.0, 10500.0, 'VALID');
            """,
            (seeded_biz,),
        )
    conn.close()

    # Trigger Job 2
    res = run_on_new_upload_check(business_id=seeded_biz, document_id=10, db_path=temp_db)
    assert res["fulfilled_requests"] == 1

    # Verify request status transitioned to FULFILLED
    open_reqs = get_document_requests(status="OPEN", business_id=seeded_biz, db_path=temp_db)
    assert not any(r["invoice_reference"] == "INV-NEEDED-99" for r in open_reqs)


def test_job3_on_changed_vat_return_reports_discrepancies(temp_db: Path, seeded_biz: int):
    """Job 3: On changed VAT return detects variance against purchase invoices."""
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        # Change filed input tax from 7,300 to 12,000 AED (now creates a 4,700 AED variance against invoices)
        conn.execute(
            "UPDATE vat_returns SET input_tax = 12000.0 WHERE business_id = ? AND period = '2021-Q1';",
            (seeded_biz,),
        )
    conn.close()

    res = run_on_changed_vat_return_check(business_id=seeded_biz, period="2021-Q1", db_path=temp_db)
    assert res["reconciled"] is False
    assert res["discrepancies_count"] >= 1

    # Check notification staged
    msgs = consume_scheduler_messages(business_id=seeded_biz, db_path=temp_db)
    assert any("difference(s)" in m["content"] for m in msgs)


def test_job4_stale_document_requests_reminds_owner(temp_db: Path, seeded_biz: int):
    """Job 4: Stale document requests (> 3 days old) triggers reminders."""
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        # Insert request requested 10 days ago
        ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
        conn.execute(
            """
            INSERT INTO document_requests (business_id, period_code, invoice_reference, supplier_name, amount_aed, reason, status, requested_at)
            VALUES (?, '2021-Q2', 'INV-STALE-1', 'Emirates Telecom', 250.0, 'Missing phone bill', 'OPEN', ?);
            """,
            (seeded_biz, ten_days_ago),
        )
    conn.close()

    res = run_stale_document_requests_check(business_id=seeded_biz, stale_days=3, db_path=temp_db)
    assert res["stale_count"] >= 1
    assert res["reminders_sent"] >= 1

    msgs = consume_scheduler_messages(business_id=seeded_biz, db_path=temp_db)
    assert any("INV-STALE-1" in m["content"] for m in msgs)


def test_job5_check_refund_applications_and_record_decision(temp_db: Path, seeded_biz: int):
    """Job 5: Check pending refund claims and record FTA approval & amount received."""
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        conn.execute(
            """
            INSERT INTO refund_records (business_id, period_codes, total_refund_claimed_aed, emartax_application_ref, submission_date, fta_status)
            VALUES (?, '["2021-Q1"]', 6300.0, 'REF-2026-0044', '2026-01-10', 'IN_REVIEW');
            """,
            (seeded_biz,),
        )
    conn.close()

    # Run check
    res = run_check_refund_applications(business_id=seeded_biz, db_path=temp_db)
    assert res["pending_claims_count"] >= 1
    assert res["reminders_sent"] >= 1

    msgs = consume_scheduler_messages(business_id=seeded_biz, db_path=temp_db)
    assert any("REF-2026-0044" in m["content"] for m in msgs)

    # Record decision (FTA Approved 6,300 AED)
    conn = get_db_connection(db_path=temp_db, readonly=True)
    cur = conn.cursor()
    cur.execute("SELECT id FROM refund_records WHERE emartax_application_ref = 'REF-2026-0044';")
    ref_id = cur.fetchone()["id"]
    conn.close()

    success = record_fta_decision(
        refund_record_id=ref_id,
        decision_status="APPROVED",
        amount_received=6300.0,
        business_id=seeded_biz,
        db_path=temp_db,
    )
    assert success is True

    # Verify audit event recorded
    conn = get_db_connection(db_path=temp_db, readonly=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM audit_events WHERE business_id = ? AND event_type = 'FTA_DECISION_RECORDED';",
        (seeded_biz,),
    )
    audit_ev = cur.fetchone()
    conn.close()
    assert audit_ev is not None


def test_job6_escalate_legal_uncertainties(temp_db: Path, seeded_biz: int):
    """Job 6: Escalate ambiguous tax classifications and NEEDS_REVIEW invoices to owner."""
    conn = get_db_connection(db_path=temp_db, readonly=False)
    with conn:
        conn.execute(
            """
            INSERT INTO invoices (business_id, direction, supplier_name, invoice_number, date, taxable_amount, vat_amount, total, eligibility_status, eligibility_reason)
            VALUES (?, 'purchase', 'VIP Catering LLC', 'INV-VIP-99', '2021-03-01', 5000.0, 250.0, 5250.0, 'NEEDS_REVIEW', 'Entertainment expense flagged by Article 53');
            """,
            (seeded_biz,),
        )
    conn.close()

    res = run_escalate_legal_uncertainties(business_id=seeded_biz, db_path=temp_db)
    assert res["flagged_count"] >= 1
    assert res["escalations_created"] >= 1

    # Verify inbox task created under waiting_user
    inbox = get_inbox_tasks(business_id=seeded_biz, db_path=temp_db)
    assert any("VIP-99" in t["description"] for t in inbox["waiting_user"])


def test_run_all_monitoring_jobs_now_records_execution_logs(temp_db: Path, seeded_biz: int):
    """Verify run_all_monitoring_jobs_now logs each job run into scheduler_runs table."""
    res = run_all_monitoring_jobs_now(business_id=seeded_biz, db_path=temp_db)
    assert "daily_check" in res
    assert "stale_documents" in res

    conn = get_db_connection(db_path=temp_db, readonly=True)
    cur = conn.cursor()
    cur.execute("SELECT job_name, status FROM scheduler_runs WHERE business_id = ?;", (seeded_biz,))
    runs = cur.fetchall()
    conn.close()

    assert len(runs) >= 4
    assert all(r["status"] == "SUCCESS" for r in runs)


def test_retry_with_backoff_on_failure(temp_db: Path, seeded_biz: int):
    """Verify exponential backoff retry mechanism handles transient failures and logs escalation."""
    attempts = 0

    def failing_job():
        nonlocal attempts
        attempts += 1
        raise ValueError("Simulated network/DB error")

    with pytest.raises(RuntimeError) as exc_info:
        _execute_with_retry(
            job_name="flaky_job",
            business_id=seeded_biz,
            func=failing_job,
            max_retries=3,
            backoff_base=0.01,
            db_path=temp_db,
        )

    assert attempts == 3
    assert "failed after 3 attempts" in str(exc_info.value)

    # Verify logged in scheduler_runs as FAILED
    conn = get_db_connection(db_path=temp_db, readonly=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM scheduler_runs WHERE business_id = ? AND job_name = 'flaky_job';",
        (seeded_biz,),
    )
    failed_run = cur.fetchone()
    conn.close()

    assert failed_run is not None
    assert failed_run["status"] == "FAILED"
