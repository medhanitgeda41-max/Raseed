"""Unit tests for analytical tools and single write gateway with rollback."""
import pytest
from database.connection import get_db_connection
from tools.read_tools import get_invoices, get_credit_ledger, get_document_requests
from tools.compute_tools import rebuild_credit_ledger
from tools.write_gateway import stage_action, execute_approved_action, reject_staged_action


def test_write_gateway_low_risk_auto_executes(temp_db):
    """Low-risk actions like REQUEST_DOCUMENT_FROM_USER execute automatically and log audit."""
    payload = {
        "period_code": "2024-Q1",
        "invoice_reference": "INV-MISSING-01",
        "supplier_name": "Logistics Gulf",
        "amount_aed": 500.0,
        "reason": "Missing tax invoice for freight charge",
    }
    action_id, status = stage_action(
        action_type="REQUEST_DOCUMENT_FROM_USER",
        payload=payload,
        reason="Detected unverified freight expense",
        source="AGENT",
        db_path=temp_db,
    )
    assert status == "AUTO_EXECUTED"
    assert action_id is not None

    # Verify document request was inserted
    requests = get_document_requests(db_path=temp_db)
    assert len(requests) == 1
    assert requests[0]["invoice_reference"] == "INV-MISSING-01"


def test_write_gateway_consequential_halts_for_approval(temp_db):
    """Consequential actions (SUBMIT_REFUND_APPLICATION) halt in PENDING_APPROVAL."""
    # Seed credit ledger with eligible amount
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (period_code, period_end_date, net_excess_aed, expiry_date, status)
            VALUES ('2024-Q1', '2024-03-31', 10000.0, '2029-03-31', 'OK');
            """
        )
    conn.close()

    payload = {"claimed_amount": 10000.0, "period_codes": ["2024-Q1"]}
    action_id, status = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload=payload,
        reason="Claim expiring credits before year end",
        source="AGENT",
        db_path=temp_db,
    )
    assert status == "PENDING_APPROVAL"

    # Execute approved action with human note
    success, msg = execute_approved_action(action_id, approver_note="Approved by CFO", db_path=temp_db)
    assert success is True
    assert "successfully executed" in msg

    # Check that credit ledger row is marked CLAIMED
    ledger = get_credit_ledger(db_path=temp_db)
    assert ledger[0]["status"] == "CLAIMED"
    assert ledger[0]["claimed_refund_amount_aed"] == 10000.0


def test_write_gateway_rollback_on_error(temp_db, monkeypatch):
    """If an error occurs mid-transaction, SQLite rolls back and leaves state untouched."""
    # Seed credit ledger
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (period_code, period_end_date, net_excess_aed, expiry_date, status)
            VALUES ('2024-Q1', '2024-03-31', 5000.0, '2029-03-31', 'OK');
            """
        )
    conn.close()

    payload = {"claimed_amount": 5000.0, "period_codes": ["2024-Q1"]}
    action_id, _ = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload=payload,
        reason="Test rollback",
        source="AGENT",
        db_path=temp_db,
    )

    # Simulate an unexpected database error during mutation execution
    from tools import write_gateway

    def crashing_mutation(cursor, action_type, payload):
        cursor.execute("UPDATE credit_ledger SET status = 'CORRUPTED';")
        raise RuntimeError("Simulated database failure or disk I/O error!")

    monkeypatch.setattr(write_gateway, "_apply_action_mutation", crashing_mutation)

    # Execution should fail gracefully
    success, msg = execute_approved_action(action_id, approver_note="Will fail", db_path=temp_db)
    assert success is False
    assert "rolled back" in msg

    # State must be completely rolled back (NOT 'CORRUPTED', NOT 'CLAIMED', still 'OK')
    ledger = get_credit_ledger(db_path=temp_db)
    assert ledger[0]["status"] == "OK"


def test_rebuild_credit_ledger_computation(temp_db, valid_vat_return_data, valid_invoice_data):
    """rebuild_credit_ledger reconciles Form 201 return output tax and verified input tax."""
    conn = get_db_connection(db_path=temp_db)
    with conn:
        # Form 201: Output VAT = 500.0, Input VAT = 1000.0
        conn.execute(
            """
            INSERT INTO vat_returns (
                period, period_start, period_end, output_tax, input_tax, net_payable_refundable
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            ("2024-Q1", "2024-01-01", "2024-03-31", 500.0, 1000.0, -500.0),
        )
        # Purchase Invoice within 2024-Q1: Taxable = 20000.0, VAT = 1000.0 (Status = VALID)
        conn.execute(
            """
            INSERT INTO invoices (
                direction, supplier_name, supplier_trn, invoice_number, date,
                taxable_amount, vat_amount, total, eligibility_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            ("purchase", "Tech Supplies", "100123456789003", "INV-101", "2024-02-15", 20000.0, 1000.0, 21000.0, "VALID"),
        )
    conn.close()

    updated = rebuild_credit_ledger(db_path=temp_db)
    assert len(updated) == 1
    row = updated[0]
    assert row["period_code"] == "2024-Q1"
    assert row["output_vat"] == 500.0
    assert row["verified_input_vat"] == 1000.0
    assert row["net_excess"] == 500.0  # 1000 - 500
    assert row["status"] in ["OK", "DUE_SOON"]
