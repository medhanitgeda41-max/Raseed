"""Unit tests for Multi-Business Data Isolation, Role-Based Access Control,
Anti-Double Counting of VAT credits, and Version-Controlled Tax Rules.
"""
import pytest
from datetime import date
from database.connection import get_db_connection, create_business, create_user
from tools.read_tools import get_invoices, list_documents, get_credit_ledger
from tools.write_gateway import stage_action, execute_approved_action
from tools.compute_tools import allocate_credit
from config.rules_loader import get_rules_for_date, get_rule_by_id
from ingest.file_storage import store_uploaded_file


def test_business_data_isolation(temp_db, temp_upload_dir):
    """Business A can never read or mutate Business B's financial data or documents."""
    # 1. Create two distinct business entities
    biz_a_id = create_business("Al Amal Trading LLC", "100111111111003", db_path=temp_db)
    biz_b_id = create_business("Baraka Motors FZE", "100222222222003", db_path=temp_db)
    assert biz_a_id != biz_b_id

    # 2. Insert invoice and document for Business A
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn, invoice_number,
                date, taxable_amount, vat_amount, total, eligibility_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (biz_a_id, "purchase", "Supplier A", "100999999999003", "INV-A-01", "2024-03-01", 1000.0, 50.0, 1050.0, "VALID"),
        )
        conn.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn, invoice_number,
                date, taxable_amount, vat_amount, total, eligibility_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (biz_b_id, "purchase", "Supplier B", "100888888888003", "INV-B-01", "2024-03-02", 2000.0, 100.0, 2100.0, "VALID"),
        )
    conn.close()

    # Upload files for each business
    store_uploaded_file(b"DOC A CONTENT", "doc_a.pdf", "invoice", business_id=biz_a_id, upload_dir=temp_upload_dir, db_path=temp_db)
    store_uploaded_file(b"DOC B CONTENT", "doc_b.pdf", "invoice", business_id=biz_b_id, upload_dir=temp_upload_dir, db_path=temp_db)

    # 3. Verify Isolation on Invoices
    invs_a = get_invoices(business_id=biz_a_id, db_path=temp_db)
    assert len(invs_a) == 1
    assert invs_a[0]["invoice_number"] == "INV-A-01"

    invs_b = get_invoices(business_id=biz_b_id, db_path=temp_db)
    assert len(invs_b) == 1
    assert invs_b[0]["invoice_number"] == "INV-B-01"

    # 4. Verify Isolation on Documents
    docs_a = list_documents(business_id=biz_a_id, db_path=temp_db)
    assert len(docs_a) == 1
    assert docs_a[0]["original_filename"] == "doc_a.pdf"
    assert f"/{biz_a_id}/" in docs_a[0]["storage_path"].replace("\\", "/")

    docs_b = list_documents(business_id=biz_b_id, db_path=temp_db)
    assert len(docs_b) == 1
    assert docs_b[0]["original_filename"] == "doc_b.pdf"
    assert f"/{biz_b_id}/" in docs_b[0]["storage_path"].replace("\\", "/")


def test_rbac_accountant_cannot_approve(temp_db):
    """An ACCOUNTANT role cannot approve consequential actions. Only OWNER or APPROVER can."""
    biz_id = create_business("Desert Star Tech", "100333333333003", db_path=temp_db)
    user_accountant_id = create_user(biz_id, "accountant_user", "ACCOUNTANT", db_path=temp_db)
    user_approver_id = create_user(biz_id, "tax_approver_user", "APPROVER", db_path=temp_db)

    # Seed eligible credit ledger
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO credit_ledger (business_id, period_code, period_end_date, net_excess_aed, expiry_date, status)
            VALUES (?, '2024-Q1', '2024-03-31', 12000.0, '2029-03-31', 'OK');
            """,
            (biz_id,),
        )
    conn.close()

    payload = {"claimed_amount": 12000.0, "period_codes": ["2024-Q1"]}
    action_id, status = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload=payload,
        reason="Claim expiring input tax",
        source="AGENT",
        business_id=biz_id,
        user_id=user_accountant_id,
        db_path=temp_db,
    )
    assert status == "PENDING_APPROVAL"

    # 1. Attempt approval as ACCOUNTANT -> Must be rejected by RBAC check!
    success, msg = execute_approved_action(
        action_id=action_id,
        approver_note="Accountant trying to approve",
        approver_role="ACCOUNTANT",
        approver_user_id=user_accountant_id,
        business_id=biz_id,
        db_path=temp_db,
    )
    assert success is False
    assert "Permission denied" in msg
    assert "ACCOUNTANT" in msg

    # 2. Approve as APPROVER -> Successfully approved!
    success_appr, msg_appr = execute_approved_action(
        action_id=action_id,
        approver_note="Signatory verified and approved",
        approver_role="APPROVER",
        approver_user_id=user_approver_id,
        business_id=biz_id,
        db_path=temp_db,
    )
    assert success_appr is True
    assert "successfully executed" in msg_appr


def test_anti_double_counting_credit_allocations(temp_db):
    """The same credit amount can never be allocated or refunded twice."""
    biz_id = create_business("Apex Logistics", "100444444444003", db_path=temp_db)

    # Insert a transaction-level credit ledger entry of 1000 AED
    conn = get_db_connection(db_path=temp_db)
    with conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO credit_ledger_entries (
                business_id, original_tax_period, original_input_vat, eligible_recoverable_vat,
                remaining_balance, expiry_date, status
            ) VALUES (?, '2024-Q1', 1000.0, 1000.0, 1000.0, '2029-03-31', 'ACTIVE');
            """,
            (biz_id,),
        )
        entry_id = cursor.lastrowid
    conn.close()

    # 1. First allocation: 600 AED to carry-forward
    res1 = allocate_credit(
        credit_ledger_entry_id=entry_id,
        allocation_type="CARRY_FORWARD_USE",
        allocated_amount=600.0,
        allocation_ref="ALLOC-REF-001",
        business_id=biz_id,
        target_period_code="2024-Q2",
        db_path=temp_db,
    )
    assert res1["allocated_amount"] == 600.0
    assert res1["remaining_balance"] == 400.0
    assert res1["status"] == "ACTIVE"

    # 2. Duplicate allocation ref should fail database UNIQUE constraint
    with pytest.raises(Exception):
        allocate_credit(
            credit_ledger_entry_id=entry_id,
            allocation_type="CARRY_FORWARD_USE",
            allocated_amount=100.0,
            allocation_ref="ALLOC-REF-001",  # Duplicate ref!
            business_id=biz_id,
            db_path=temp_db,
        )

    # 3. Over-allocation (attempting 500 AED when only 400 AED remains)
    with pytest.raises(ValueError) as exc:
        allocate_credit(
            credit_ledger_entry_id=entry_id,
            allocation_type="REFUND_CLAIM",
            allocated_amount=500.0,
            allocation_ref="ALLOC-REF-002",
            business_id=biz_id,
            db_path=temp_db,
        )
    assert "Double counting prevented" in str(exc.value)

    # 4. Final remaining allocation of exact 400 AED
    res2 = allocate_credit(
        credit_ledger_entry_id=entry_id,
        allocation_type="REFUND_CLAIM",
        allocated_amount=400.0,
        allocation_ref="ALLOC-REF-003",
        business_id=biz_id,
        db_path=temp_db,
    )
    assert res2["remaining_balance"] == 0.0
    assert res2["status"] == "USED"


def test_version_controlled_rules_loader():
    """Rules loader dynamically returns only the rules in effect on the transaction date."""
    # 1. On 2024-01-01:
    # Standard rate (from 2018-01-01) is active.
    # 5-year expiry (effective 2026-01-01) is NOT active yet.
    rules_2024 = get_rules_for_date("2024-01-01")
    ids_2024 = [r["id"] for r in rules_2024]
    assert "RULE_STANDARD_VAT_RATE" in ids_2024
    assert "RULE_STATUTORY_EXPIRY_5YR" not in ids_2024
    assert "RULE_TRANSITIONAL_REFUND_DEADLINE" not in ids_2024

    # 2. On 2026-06-01:
    # Both 5-year expiry rule and transitional deadline are active.
    rules_2026 = get_rules_for_date("2026-06-01")
    ids_2026 = [r["id"] for r in rules_2026]
    assert "RULE_STANDARD_VAT_RATE" in ids_2026
    assert "RULE_STATUTORY_EXPIRY_5YR" in ids_2026
    assert "RULE_TRANSITIONAL_REFUND_DEADLINE" in ids_2026

    # 3. On 2027-01-01:
    # Transitional relief expired on 2026-12-31, so it is NO LONGER active!
    rules_2027 = get_rules_for_date("2027-01-01")
    ids_2027 = [r["id"] for r in rules_2027]
    assert "RULE_STANDARD_VAT_RATE" in ids_2027
    assert "RULE_STATUTORY_EXPIRY_5YR" in ids_2027
    assert "RULE_TRANSITIONAL_REFUND_DEADLINE" not in ids_2027
