"""Unit tests for Part 3: Better column matching for uploaded registers."""
import io
import json
import pytest
import pandas as pd
from pathlib import Path

from database.connection import init_db, get_db_connection
from ingest.excel_parser import (
    guess_column_mapping,
    fuzzy_match_header,
    detect_swapped_amount_and_vat,
    verify_values_and_refine,
    save_cached_column_mapping,
    get_cached_column_mapping,
    parse_excel_register,
    CANONICAL_COLUMNS,
    CANONICAL_ALIASES,
)


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_columns.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DB_PATH", str(db_file))
    init_db(db_path=db_file)
    conn = get_db_connection(db_path=db_file)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO businesses (id, name, trn) VALUES (1, 'Test Corp 1', '100123456789003');"
        )
        conn.execute(
            "INSERT OR IGNORE INTO businesses (id, name, trn) VALUES (2, 'Test Corp 2', '100987654321003');"
        )
    conn.close()
    return db_file


def test_canonical_synonyms_english():
    """Verify common English invoice register variations are recognized."""
    headers = [
        "Voucher No",
        "Trans Date",
        "Party Name",
        "Tax Reg No",
        "Taxable Val",
        "Vat 5%",
        "Gross Total",
    ]
    mapping, conf, unmapped = guess_column_mapping(columns=headers)
    assert mapping["invoice_number"] == "Voucher No"
    assert mapping["date"] == "Trans Date"
    assert mapping["supplier_name"] == "Party Name"
    assert mapping["supplier_trn"] == "Tax Reg No"
    assert mapping["taxable_amount"] == "Taxable Val"
    assert mapping["vat_amount"] == "Vat 5%"
    assert mapping["total"] == "Gross Total"
    assert conf == 1.0
    assert len(unmapped) == 0


def test_canonical_synonyms_arabic():
    """Verify standard Arabic invoice register headers are recognized."""
    headers = [
        "رقم الفاتورة",
        "تاريخ الفاتورة",
        "اسم المورد",
        "الرقم الضريبي للمورد",
        "المبلغ الخاضع للضريبة",
        "مبلغ الضريبة",
        "المبلغ الإجمالي",
    ]
    mapping, conf, unmapped = guess_column_mapping(columns=headers)
    assert mapping["invoice_number"] == "رقم الفاتورة"
    assert mapping["date"] == "تاريخ الفاتورة"
    assert mapping["supplier_name"] == "اسم المورد"
    assert mapping["supplier_trn"] == "الرقم الضريبي للمورد"
    assert mapping["taxable_amount"] == "المبلغ الخاضع للضريبة"
    assert mapping["vat_amount"] == "مبلغ الضريبة"
    assert mapping["total"] == "المبلغ الإجمالي"
    assert conf == 1.0
    assert len(unmapped) == 0


def test_fuzzy_matching_difflib():
    """Verify SequenceMatcher tolerates typos and informal header abbreviations."""
    score1 = fuzzy_match_header("invoce no", "invoice_number")
    assert score1 >= 0.70

    score2 = fuzzy_match_header("taxbl amount", "taxable_amount")
    assert score2 >= 0.70

    # Header list with slight typos not matched by exact patterns
    headers = [
        "invoce num",
        "transac date",
        "suplr name",
        "taxble val",
        "input tax",
    ]
    mapping, conf, unmapped = guess_column_mapping(columns=headers)
    assert mapping.get("invoice_number") == "invoce num"
    assert mapping.get("taxable_amount") == "taxble val"
    assert mapping.get("vat_amount") == "input tax"


def test_swapped_amount_and_vat_heuristic():
    """Detect and auto-correct swapped Taxable Amount and VAT Amount."""
    # Suppose user labelled 'taxable_amount' as the 5% VAT (50, 100, 25)
    # and 'vat_amount' as the base taxable amount (1000, 2000, 500)
    data = {
        "invoice_number": ["INV-001", "INV-002", "INV-003"],
        "date": ["2024-01-15", "2024-01-16", "2024-01-17"],
        "taxable_amount": [50.0, 100.0, 25.0],      # Swapped! Real VAT
        "vat_amount": [1000.0, 2000.0, 500.0],       # Swapped! Real Taxable
    }
    df = pd.DataFrame(data)

    # detect_swapped_amount_and_vat returns True
    assert detect_swapped_amount_and_vat(df, "taxable_amount", "vat_amount") is True

    # guess_column_mapping should auto-swap them
    mapping, conf, unmapped = guess_column_mapping(columns=list(df.columns), df=df)
    assert mapping["taxable_amount"] == "vat_amount"
    assert mapping["vat_amount"] == "taxable_amount"


def test_trn_and_date_value_heuristics():
    """Verify heuristics infer unmapped 15-digit TRNs and dates from data values."""
    data = {
        "Col_A": ["INV-101", "INV-102", "INV-103"],
        "Col_B": ["2024-02-01", "2024-02-02", "2024-02-03"],  # Dates
        "Col_C": [1000.0, 2000.0, 3000.0],
        "Col_D": [50.0, 100.0, 150.0],
        "Col_E": ["100123456789003", "100987654321003", "100555666777003"],  # UAE TRNs
    }
    df = pd.DataFrame(data)

    mapping = {
        "invoice_number": "Col_A",
        "taxable_amount": "Col_C",
        "vat_amount": "Col_D",
    }
    matched = {"Col_A", "Col_C", "Col_D"}
    refined = verify_values_and_refine(df, mapping, matched)

    assert refined.get("supplier_trn") == "Col_E"
    assert refined.get("date") == "Col_B"


def test_cached_column_mapping_persistence(test_db):
    """Verify saving and retrieving company-specific column layout patterns."""
    headers = ["Custom_Bill_ID", "Custom_Day", "Custom_Net", "Custom_Tax"]
    confirmed_mapping = {
        "invoice_number": "Custom_Bill_ID",
        "date": "Custom_Day",
        "taxable_amount": "Custom_Net",
        "vat_amount": "Custom_Tax",
    }

    # Initially, no cache for business 1
    cached = get_cached_column_mapping(business_id=1, headers=headers, db_path=test_db)
    assert cached is None

    # Save mapping for business 1
    save_cached_column_mapping(
        business_id=1,
        headers=headers,
        mapping=confirmed_mapping,
        db_path=test_db,
    )

    # Retrieve mapping for business 1
    cached_after = get_cached_column_mapping(business_id=1, headers=headers, db_path=test_db)
    assert cached_after == confirmed_mapping

    # Calling guess_column_mapping with business 1 uses cached pattern with 1.0 confidence
    mapping, conf, unmapped = guess_column_mapping(
        columns=headers,
        business_id=1,
        db_path=test_db,
    )
    assert mapping == confirmed_mapping
    assert conf == 1.0
    assert len(unmapped) == 0

    # Business 2 does not share business 1's cached mapping
    biz2_cached = get_cached_column_mapping(business_id=2, headers=headers, db_path=test_db)
    assert biz2_cached is None


def test_parse_excel_register_with_arabic_and_custom_override(test_db):
    """Test full parsing of CSV buffer with Arabic columns and custom mapping override."""
    csv_content = (
        "رقم الفاتورة,تاريخ الفاتورة,اسم المورد,الرقم الضريبي للمورد,المبلغ الخاضع للضريبة,مبلغ الضريبة,المبلغ الإجمالي\n"
        "INV-AR-001,2024-03-01,شركة الأمل,100123456789003,10000.00,500.00,10500.00\n"
        "INV-AR-002,2024-03-05,مؤسسة النور,100987654321003,5000.00,250.00,5250.00\n"
    ).encode("utf-8")

    records, unmapped = parse_excel_register(
        file_bytes=csv_content,
        filename="arabic_register.csv",
        business_id=1,
        db_path=test_db,
    )

    assert len(records) == 2
    assert len(unmapped) == 0
    assert records[0]["invoice_number"] == "INV-AR-001"
    assert records[0]["supplier_name"] == "شركة الأمل"
    assert records[0]["supplier_trn"] == "100123456789003"
    assert records[0]["taxable_amount"] == 10000.00
    assert records[0]["vat_amount"] == 500.00
    assert records[0]["total"] == 10500.00
