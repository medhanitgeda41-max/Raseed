"""Unit tests for official FTA VAT refund package generation (Excel and PDF)."""
from database.connection import get_db_connection  # engine-agnostic test connection
from pathlib import Path
import pytest
import sqlite3
from tools.package_exporter import generate_refund_excel_package, generate_refund_pdf_package
from database.connection import init_db


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "exporter_test.db"
    init_db(db_file)
    return db_file


def test_generate_refund_excel_package(temp_db: Path):
    """Excel export generates valid workbook with summary and invoice rows."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'Excel Export LLC', '100123456789001', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Trading', 'Dubai', 'Rashid', 'rashid@test.com', 'AE****************1111');
        """
    )
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Vendor Tech', '100999888777666', 'INV-EXCEL-01', '2024-02-15',
                  10000.0, 500.0, 10500.0, 'AED', '["Servers"]', 'VALID');
        """
    )
    conn.commit()
    conn.close()

    excel_bytes = generate_refund_excel_package(
        business_id=1,
        period_codes=["2024-Q1"],
        claimed_amount=500.0,
        db_path=temp_db,
    )
    assert len(excel_bytes) > 1000
    # PK zip signature of .xlsx
    assert excel_bytes[:2] == b"PK"


def test_generate_refund_pdf_package(temp_db: Path):
    """PDF export generates formal dossier with PDF header."""
    conn = get_db_connection(db_path=temp_db)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO business_profile (
            business_id, legal_name, trn, vat_registration_date, tax_period_type,
            first_tax_period_start, business_activity, emirate, signatory_name, signatory_email, masked_iban
        ) VALUES (1, 'PDF Export LLC', '100123456789002', '2020-01-01', 'quarterly',
                  '2024-01-01', 'Consulting', 'Abu Dhabi', 'Fatima', 'fatima@test.com', 'AE****************2222');
        """
    )
    cur.execute(
        """
        INSERT INTO invoices (
            business_id, direction, supplier_name, supplier_trn, invoice_number, date,
            taxable_amount, vat_amount, total, currency, description_lines, eligibility_status
        ) VALUES (1, 'purchase', 'Advisory Gulf', '100888777666555', 'INV-PDF-01', '2024-02-10',
                  20000.0, 1000.0, 21000.0, 'AED', '["Advisory fees"]', 'VALID');
        """
    )
    conn.commit()
    conn.close()

    pdf_bytes = generate_refund_pdf_package(
        business_id=1,
        period_codes=["2024-Q1"],
        claimed_amount=1000.0,
        db_path=temp_db,
    )
    assert len(pdf_bytes) > 1000
    # PDF magic signature %PDF
    assert pdf_bytes[:4] == b"%PDF"
