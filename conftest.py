"""Pytest root fixtures for Raseed."""
import tempfile
import pytest
from pathlib import Path
from datetime import date, timedelta
from database.connection import init_db


@pytest.fixture
def temp_db(tmp_path):
    """Provide a fresh, isolated SQLite test database with all tables initialized."""
    db_file = tmp_path / "test_raseed.db"
    init_db(db_file)
    return db_file


@pytest.fixture
def temp_upload_dir(tmp_path):
    """Provide an isolated upload directory."""
    upload_path = tmp_path / "uploads"
    upload_path.mkdir(parents=True, exist_ok=True)
    return upload_path


@pytest.fixture
def valid_profile_data():
    """Return dictionary of valid business profile fields."""
    return {
        "legal_name": "Al Amal Trading LLC",
        "trn": "100123456789003",
        "vat_registration_date": date.today() - timedelta(days=365),
        "tax_period_type": "quarterly",
        "first_tax_period_start": date.today() - timedelta(days=365),
        "business_activity": "Wholesale and Retail Trading",
        "emirate": "Dubai",
        "makes_exempt_supplies": False,
        "signatory_name": "Fatima Al Mansoori",
        "signatory_email": "fatima.mansoori@alamal.ae",
        "masked_iban": "AE290330000000001234567",
    }


@pytest.fixture
def valid_invoice_data():
    """Return dictionary of valid tax invoice fields."""
    return {
        "direction": "purchase",
        "supplier_name": "Office Supplies Gulf FZE",
        "supplier_trn": "100987654321003",
        "customer_name": "Al Amal Trading LLC",
        "customer_trn": "100123456789003",
        "invoice_number": "INV-2024-0091",
        "date": date.today() - timedelta(days=30),
        "taxable_amount": 1000.0,
        "vat_amount": 50.0,
        "total": 1050.0,
        "currency": "AED",
        "description_lines": ["Ergonomic office chairs and desks"],
        "source_document": "invoice_0091.pdf",
        "extraction_confidence": 0.98,
        "eligibility_status": "VALID",
    }


@pytest.fixture
def valid_vat_return_data():
    """Return dictionary of valid Form 201 VAT return fields."""
    start = date(2024, 1, 1)
    end = date(2024, 3, 31)
    return {
        "period": "2024-Q1",
        "period_start": start,
        "period_end": end,
        "output_tax": 25000.0,
        "input_tax": 40000.0,
        "net_payable_refundable": -15000.0,  # Negative indicates refundable credit
        "filing_date": date(2024, 4, 25),
    }
