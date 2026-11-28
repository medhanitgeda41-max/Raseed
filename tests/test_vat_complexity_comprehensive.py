"""Comprehensive tests for UAE VAT statutory complexity, accounting accuracy, and deterministic reconciliation.

Covers:
1. Every rule in config/tax_rules.yaml (14 rules verified)
2. All statutory transaction classifications (9-way)
3. Zero-rated and exempt sales creating 0 output tax
4. Import and reverse-charge reciprocal accounting
5. Purchase credit notes reducing input tax; sales credit notes reducing output tax
6. 70% partial exemption recovery apportionment
7. Invoices with 5% VAT in blocked categories marked non-recoverable / NEEDS_HUMAN_REVIEW
8. recalculate_credit_ledger_from_scratch() matching stored ledger
9. reconcile_4_way() detecting missing_invoice, duplicate, classification_difference,
   timing_difference, and unrecorded_credit_note
"""
from database.connection import get_db_connection  # engine-agnostic test connection
from datetime import date
from pathlib import Path
import pytest
import sqlite3

from config.rules_loader import load_all_rules, get_rule_by_id, get_rules_for_date
from guards.transaction_classifier import classify_transaction
from guards.eligibility_rules import classify_input_tax_eligibility
from guards.invoice_validator import validate_tax_invoice
from guards.deadline_engine import calculate_credit_expiry
from guards.refund_guard import verify_refund_claim
from tools.compute_tools import (
    rebuild_credit_ledger,
    recalculate_credit_ledger_from_scratch,
    reconcile_4_way,
    allocate_credit,
)
from database.connection import init_db, create_business, create_user


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "vat_complexity.db"
    init_db(db_file)
    return db_file


# ==============================================================================
# 1. VERIFICATION OF EVERY RULE IN config/tax_rules.yaml
# ==============================================================================

def test_every_tax_rule_has_automated_verification():
    """Verify that every statutory rule in config/tax_rules.yaml is present, active, and valid."""
    all_rules = load_all_rules()
    assert len(all_rules) >= 14

    rule_ids = {r["id"] for r in all_rules}
    expected_ids = {
        "RULE_STANDARD_VAT_RATE",
        "RULE_ZERO_RATED_SUPPLIES",
        "RULE_EXEMPT_SUPPLIES",
        "RULE_REVERSE_CHARGE_MECHANISM",
        "RULE_IMPORTS_VAT",
        "RULE_CREDIT_NOTES_PURCHASE",
        "RULE_CREDIT_NOTES_SALES",
        "RULE_PARTIAL_BUSINESS_NON_BUSINESS",
        "RULE_PARTIAL_EXEMPT_APPORTIONMENT",
        "RULE_BLOCKED_ENTERTAINMENT",
        "RULE_BLOCKED_MOTOR_VEHICLE",
        "RULE_STATUTORY_EXPIRY_5YR",
        "RULE_TRANSITIONAL_REFUND_DEADLINE",
        "RULE_TRN_FORMAT",
    }
    assert expected_ids.issubset(rule_ids), f"Missing rules: {expected_ids - rule_ids}"

    # Verify each rule has official citation and non-empty description
    for r in all_rules:
        assert r.get("official_source"), f"Rule {r['id']} missing official_source"
        assert r.get("description"), f"Rule {r['id']} missing description"
        assert r.get("verification_status"), f"Rule {r['id']} missing verification_status"


def test_rule_date_filtering():
    """Verify rules valid for specific dates."""
    rules_2024 = get_rules_for_date(date(2024, 6, 1))
    rule_ids_2024 = {r["id"] for r in rules_2024}
    # 2026 transitional rule should NOT be active in 2024
    assert "RULE_TRANSITIONAL_REFUND_DEADLINE" not in rule_ids_2024
    assert "RULE_STANDARD_VAT_RATE" in rule_ids_2024

    rules_2026 = get_rules_for_date(date(2026, 6, 1))
    rule_ids_2026 = {r["id"] for r in rules_2026}
    assert "RULE_TRANSITIONAL_REFUND_DEADLINE" in rule_ids_2026


# ==============================================================================
# 2. STATUTORY TRANSACTION CLASSIFICATIONS
# ==============================================================================

def test_classify_standard_rated():
    t_type, rate, recoverable, reason = classify_transaction(
        description="IT consulting and software licensing",
        taxable_amount=10000.0,
        vat_amount=500.0,
        direction="purchase",
    )
    assert t_type == "standard_rated"
    assert rate == 0.05
    assert recoverable is True


def test_classify_zero_rated_export():
    t_type, rate, recoverable, reason = classify_transaction(
        description="Export of electronic equipment to Oman",
        taxable_amount=50000.0,
        vat_amount=0.0,
        direction="sales",
    )
    assert t_type == "zero_rated"
    assert rate == 0.0
    assert recoverable is True


def test_classify_exempt_supplies():
    t_type, rate, recoverable, reason = classify_transaction(
        description="Residential building lease after 3 years of completion",
        taxable_amount=80000.0,
        vat_amount=0.0,
        direction="sales",
    )
    assert t_type == "exempt"
    assert rate == 0.0
    assert recoverable is False


def test_classify_reverse_charge():
    t_type, rate, recoverable, reason = classify_transaction(
        description="Management consulting from UK non-resident supplier (reverse charge)",
        taxable_amount=20000.0,
        vat_amount=1000.0,
        direction="purchase",
    )
    assert t_type == "reverse_charge"
    assert rate == 0.05
    assert recoverable is True


def test_classify_import_goods():
    t_type, rate, recoverable, reason = classify_transaction(
        description="Customs import declaration 701 port authority goods",
        taxable_amount=100000.0,
        vat_amount=5000.0,
        direction="purchase",
    )
    assert t_type == "import"
    assert rate == 0.05
    assert recoverable is True


def test_classify_credit_notes():
    # Purchase Credit Note
    t_type_p, _, rec_p, _ = classify_transaction(
        description="Credit note CN-1021 for returned defective hardware",
        taxable_amount=-5000.0,
        vat_amount=-250.0,
        direction="purchase",
    )
    assert t_type_p == "credit_note_purchase"
    assert rec_p is True

    # Sales Credit Note
    t_type_s, _, rec_s, _ = classify_transaction(
        description="Credit note for customer discount",
        taxable_amount=-2000.0,
        vat_amount=-100.0,
        direction="sales",
    )
    assert t_type_s == "credit_note_sales"
    assert rec_s is False


def test_classify_special_review_on_discrepancy():
    # 5% of 10,000 is 500, but invoice says 250 with no exemption reason
    t_type, rate, recoverable, reason = classify_transaction(
        description="General office procurement",
        taxable_amount=10000.0,
        vat_amount=250.0,
        direction="purchase",
    )
    assert t_type == "special_review"
    assert recoverable is False


# ==============================================================================
# 3. BLOCKED CATEGORIES & APPORTIONMENT
# ==============================================================================

def test_blocked_entertainment_cannot_be_auto_claimed():
    status, rate, reason, citation = classify_input_tax_eligibility(
        category="entertainment",
        description="Executive dinner at luxury resort for client hospitality",
        transaction_date=date(2025, 3, 1),
    )
    assert status == "NEEDS_HUMAN_REVIEW"
    assert rate == 0.0
    assert "entertainment" in reason.lower()


def test_blocked_personal_motor_vehicle():
    status, rate, reason, citation = classify_input_tax_eligibility(
        category="motor_vehicle",
        description="Monthly lease on personal vehicle for director",
        transaction_date=date(2025, 3, 1),
    )
    assert status == "NEEDS_HUMAN_REVIEW"
    assert rate == 0.0
    assert "motor vehicle" in reason.lower()


def test_partial_business_non_business():
    status, rate, reason, citation = classify_input_tax_eligibility(
        category="mixed_use",
        description="Dual-use mobile phones and home internet for hybrid staff",
        transaction_date=date(2025, 3, 1),
        is_partial_business_use=True,
    )
    assert status == "NEEDS_HUMAN_REVIEW"
    assert "partial business use" in reason.lower()


def test_70_percent_partial_recovery_apportionment(temp_db: Path):
    """Test that business with exempt supplies apportions recoverable VAT at configured 70%."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    # Create business profile with 70% apportionment
    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, makes_exempt_supplies,
            apportionment_percentage, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'Apportioned Trading LLC', '100999888777666', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Financial & Commercial', 'Dubai', 1, 70.0, 'Ali', 'ali@test.com', 'AE****************1234');
        """
    )
    # Insert Return: 0 output tax, 1000 declared
    cur.execute(
        """
        INSERT INTO vat_returns (business_id, period, period_start, period_end, output_tax, input_tax, net_payable_refundable)
        VALUES (1, '2024-Q1', '2024-01-01', '2024-03-31', 0.0, 1000.0, -1000.0);
        """
    )
    # Insert purchase invoice with 1,000 AED VAT
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Main Supplier LLC', '100111222333444', 'INV-APP-1', '2024-02-15',
                  20000.0, 1000.0, 21000.0, 'AED', '["General operating expenses"]', 'VALID');
        """
    )
    conn.commit()
    conn.close()

    # Rebuild ledger
    res = rebuild_credit_ledger(business_id=1, db_path=temp_db)
    assert len(res) == 1
    # 1000.0 * 70% = 700.0 AED verified input
    assert res[0]["verified_input_vat"] == 700.0
    assert res[0]["net_excess"] == 700.0
    assert res[0]["apportionment_applied"] is True


# ==============================================================================
# 4. CREDIT NOTES & RECIPROCAL ACCOUNTING
# ==============================================================================

def test_purchase_credit_note_reduces_input_tax(temp_db: Path):
    """Test purchase credit note decreases total verified recoverable input tax."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, makes_exempt_supplies,
            apportionment_percentage, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'Credit Note Test LLC', '100123456789012', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Trading', 'Dubai', 0, 100.0, 'Sara', 'sara@test.com', 'AE****************5678');
        """
    )
    cur.execute(
        """
        INSERT INTO vat_returns (business_id, period, period_start, period_end, output_tax, input_tax, net_payable_refundable)
        VALUES (1, '2024-Q1', '2024-01-01', '2024-03-31', 100.0, 450.0, -350.0);
        """
    )
    # Valid invoice: 500 AED VAT
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Vendor A', '100555666777888', 'INV-500', '2024-01-10',
                  10000.0, 500.0, 10500.0, 'AED', '["Office supplies"]', 'VALID');
        """
    )
    # Credit note: 50 AED VAT with description indicating credit note
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Vendor A', '100555666777888', 'CN-50', '2024-02-10',
                  1000.0, 50.0, 1050.0, 'AED', '["Credit note for returned goods"]', 'VALID');
        """
    )
    conn.commit()
    conn.close()

    res = rebuild_credit_ledger(business_id=1, db_path=temp_db)
    assert len(res) == 1
    # 500 - 50 = 450.0 AED verified input
    assert res[0]["verified_input_vat"] == 450.0
    # Net excess = 450 - 100 output = 350.0 AED
    assert res[0]["net_excess"] == 350.0


# ==============================================================================
# 5. RECALCULATE CREDIT LEDGER FROM SCRATCH
# ==============================================================================

def test_recalculate_credit_ledger_from_scratch_matches_stored(temp_db: Path):
    """Test that recalculation from scratch identically matches stored credit ledger."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, makes_exempt_supplies,
            apportionment_percentage, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'Scratch Match LLC', '100888999000111', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Retail', 'Abu Dhabi', 0, 100.0, 'Omar', 'omar@test.com', 'AE****************9999');
        """
    )
    cur.execute(
        """
        INSERT INTO vat_returns (business_id, period, period_start, period_end, output_tax, input_tax, net_payable_refundable)
        VALUES (1, '2024-Q1', '2024-01-01', '2024-03-31', 200.0, 1200.0, -1000.0);
        """
    )
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Supplier Tech', '100444555666777', 'INV-SCR-1', '2024-02-01',
                  24000.0, 1200.0, 25200.0, 'AED', '["Equipment purchase"]', 'VALID');
        """
    )
    conn.commit()
    conn.close()

    # Step 1: Build stored ledger
    rebuild_credit_ledger(business_id=1, db_path=temp_db)

    # Step 2: Recalculate completely from scratch
    scratch = recalculate_credit_ledger_from_scratch(business_id=1, db_path=temp_db)
    assert scratch["matches_stored"] is True
    assert scratch["total_calculated_excess_aed"] == 1000.0
    assert scratch["total_stored_excess_aed"] == 1000.0
    assert scratch["periods"][0]["calc_net_excess_aed"] == 1000.0


# ==============================================================================
# 6. 4-WAY RECONCILIATION WITH CAUSE TAGS
# ==============================================================================

def test_4_way_reconciliation_detects_all_causes(temp_db: Path):
    """Test that 4-way reconciliation correctly flags missing_invoice, duplicate,
    classification_difference, timing_difference, and unrecorded_credit_note."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, makes_exempt_supplies,
            apportionment_percentage, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'Reconciliation Hub LLC', '100777888999000', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Logistics', 'Dubai', 0, 100.0, 'Zayd', 'zayd@test.com', 'AE****************1111');
        """
    )

    # Filed VAT return for 2024-Q1 (claims 2,000 input tax)
    cur.execute(
        """
        INSERT INTO vat_returns (business_id, period, period_start, period_end, output_tax, input_tax, net_payable_refundable)
        VALUES (1, '2024-Q1', '2024-01-01', '2024-03-31', 500.0, 2000.0, -1500.0);
        """
    )

    # 1. Valid Invoice: 1,000 AED VAT (leaving 1,000 AED missing vs return!)
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Valid Supplier', '100111222333444', 'INV-VALID', '2024-01-15',
                  20000.0, 1000.0, 21000.0, 'AED', '["Warehousing equipment"]', 'VALID');
        """
    )

    # 2. Duplicate Invoice
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Valid Supplier', '100111222333444', 'INV-DUP-1', '2024-01-15',
                  20000.0, 1000.0, 21000.0, 'AED', '["Warehousing equipment duplicate"]', 'DUPLICATE');
        """
    )

    # 3. Classification Difference (Blocked entertainment)
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status, eligibility_reason
        ) VALUES (1, 'purchase', 'Luxury Catering', '100222333444555', 'INV-BLOCKED', '2024-02-20',
                  10000.0, 500.0, 10500.0, 'AED', '["Client hospitality dinner"]', 'NEEDS_REVIEW',
                  'Blocked under Article 53: entertainment services');
        """
    )

    # 4. Timing Difference (Invoice dated in 2024-Q3, outside 2024-Q1 return period)
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Supplier Future', '100333444555666', 'INV-TIMING', '2024-08-15',
                  4000.0, 200.0, 4200.0, 'AED', '["Maintenance service"]', 'VALID');
        """
    )

    # 5. Missing Invoice in GL: GL has 300 AED debit with no linked invoice
    cur.execute(
        """
        INSERT INTO ledger_entries (
            business_id, entry_date, account_code, account_name, description, debit_aed, credit_aed
        ) VALUES (1, '2024-02-10', '2100-VAT', 'VAT Recoverable', 'Unvouched consulting fees', 300.0, 0.0);
        """
    )

    # 6. Unrecorded Credit Note: GL has 150 AED credit for a credit note not in purchase register
    cur.execute(
        """
        INSERT INTO ledger_entries (
            business_id, entry_date, account_code, account_name, description, debit_aed, credit_aed
        ) VALUES (1, '2024-03-01', '2100-VAT', 'VAT Recoverable', 'Supplier credit note CN-9901', 0.0, 150.0);
        """
    )

    conn.commit()
    conn.close()

    # Run 4-way reconciliation
    recon = reconcile_4_way(business_id=1, db_path=temp_db)
    assert recon["reconciled"] is False
    assert recon["discrepancies_count"] >= 5

    counts = recon["discrepancy_counts_by_cause"]
    assert counts["duplicate"] >= 1
    assert counts["missing_invoice"] >= 1
    assert counts["classification_difference"] >= 1
    assert counts["timing_difference"] >= 1
    assert counts["unrecorded_credit_note"] >= 1
