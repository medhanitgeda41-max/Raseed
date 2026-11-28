"""Unit tests for onboarding checklist and status evaluator."""
from datetime import date
from database.connection import get_db_connection
from onboarding.requirements import onboarding_status, validate_minimum_onboarding


def test_onboarding_status_empty_database_cannot_start(temp_db):
    """Zero fake data test: an empty database must not start and must list missing items."""
    status = onboarding_status(db_path=temp_db)
    assert status["can_start"] is False
    assert validate_minimum_onboarding(db_path=temp_db) is False

    # Check that all 3 mandatory requirements are reported missing
    assert "business_profile" in status["missing_required"]
    assert "purchase_invoices" in status["missing_required"]
    assert "filed_vat_returns" in status["missing_required"]
    assert len(status["present"]) == 0
    assert status["progress_percent"] == 0.0
    assert "Cannot start calculation" in status["summary"]


def test_onboarding_partial_progress(temp_db, valid_profile_data):
    """Adding business profile should move it to present, but still block start."""
    conn = get_db_connection(db_path=temp_db)
    with conn:
        conn.execute(
            """
            INSERT INTO business_profile (
                legal_name, trn, vat_registration_date, tax_period_type,
                first_tax_period_start, business_activity, emirate,
                makes_exempt_supplies, signatory_name, signatory_email, masked_iban
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                valid_profile_data["legal_name"],
                valid_profile_data["trn"],
                valid_profile_data["vat_registration_date"].isoformat(),
                valid_profile_data["tax_period_type"],
                valid_profile_data["first_tax_period_start"].isoformat(),
                valid_profile_data["business_activity"],
                valid_profile_data["emirate"],
                1 if valid_profile_data["makes_exempt_supplies"] else 0,
                valid_profile_data["signatory_name"],
                valid_profile_data["signatory_email"],
                valid_profile_data["masked_iban"],
            ),
        )
    conn.close()

    status = onboarding_status(db_path=temp_db)
    assert status["can_start"] is False
    assert "business_profile" in status["present"]
    assert "purchase_invoices" in status["missing_required"]
    assert "filed_vat_returns" in status["missing_required"]
    assert status["progress_percent"] > 0.0


def test_onboarding_minimum_met_authorizes_start(temp_db, valid_profile_data, valid_invoice_data, valid_vat_return_data):
    """Providing Profile + Purchase Invoices + Filed VAT Returns unlocks the agent."""
    conn = get_db_connection(db_path=temp_db)
    with conn:
        # 1. Profile
        conn.execute(
            """
            INSERT INTO business_profile (
                legal_name, trn, vat_registration_date, tax_period_type,
                first_tax_period_start, business_activity, emirate,
                makes_exempt_supplies, signatory_name, signatory_email, masked_iban
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                valid_profile_data["legal_name"],
                valid_profile_data["trn"],
                valid_profile_data["vat_registration_date"].isoformat(),
                valid_profile_data["tax_period_type"],
                valid_profile_data["first_tax_period_start"].isoformat(),
                valid_profile_data["business_activity"],
                valid_profile_data["emirate"],
                0,
                valid_profile_data["signatory_name"],
                valid_profile_data["signatory_email"],
                valid_profile_data["masked_iban"],
            ),
        )
        # 2. Purchase Invoice
        conn.execute(
            """
            INSERT INTO invoices (
                direction, supplier_name, supplier_trn, customer_name, customer_trn,
                invoice_number, date, taxable_amount, vat_amount, total, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                "purchase",
                valid_invoice_data["supplier_name"],
                valid_invoice_data["supplier_trn"],
                valid_invoice_data["customer_name"],
                valid_invoice_data["customer_trn"],
                valid_invoice_data["invoice_number"],
                valid_invoice_data["date"].isoformat(),
                valid_invoice_data["taxable_amount"],
                valid_invoice_data["vat_amount"],
                valid_invoice_data["total"],
                "AED",
            ),
        )
        # 3. Form 201 VAT Return
        conn.execute(
            """
            INSERT INTO vat_returns (
                period, period_start, period_end, output_tax, input_tax, net_payable_refundable
            ) VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                valid_vat_return_data["period"],
                valid_vat_return_data["period_start"].isoformat(),
                valid_vat_return_data["period_end"].isoformat(),
                valid_vat_return_data["output_tax"],
                valid_vat_return_data["input_tax"],
                valid_vat_return_data["net_payable_refundable"],
            ),
        )
    conn.close()

    status = onboarding_status(db_path=temp_db)
    assert status["can_start"] is True
    assert validate_minimum_onboarding(db_path=temp_db) is True
    assert len(status["missing_required"]) == 0
    assert "Agent is authorized" in status["summary"]
