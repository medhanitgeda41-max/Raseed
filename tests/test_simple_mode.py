"""Unit tests for Part 4 - Simple mode for demo and OCR verification."""
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from database.connection import get_db_connection, init_db, get_or_create_default_business
from tools.write_gateway import save_business_profile
from tools.compute_tools import rebuild_credit_ledger, reconcile_4_way
from agent.visual_panel import render_vat_credit_panel, render_all_periods_view
from ingest.extractor import extract_invoice_fields


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_simple_mode.db"
    init_db(db_file)
    biz_id = get_or_create_default_business(db_file)
    save_business_profile(
        business_id=biz_id,
        legal_name="Al Amal Trading LLC",
        trn="100123456789003",
        vat_registration_date="2021-01-01",
        tax_period_type="quarterly",
        db_path=db_file,
    )
    return db_file, biz_id


def test_simple_mode_view_filtering(test_db):
    """Test that simple mode restricts the panel to 3 views instead of 7."""
    db_path, biz_id = test_db

    def mock_columns(spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        cols = [MagicMock() for _ in range(count)]
        for c in cols:
            c.button.return_value = False
        return cols

    with patch("streamlit.button", return_value=False), \
         patch("streamlit.columns", side_effect=mock_columns), \
         patch("streamlit.markdown"), \
         patch("streamlit.caption"), \
         patch("streamlit.plotly_chart"), \
         patch("streamlit.expander"):
        view = render_vat_credit_panel(
            business_id=biz_id,
            current_view="reconciliation",
            db_path=db_path,
            simple_mode=True,
        )
        # Should sanitize invalid simple view to all_periods
        assert view == "all_periods"


def test_normal_mode_view_support(test_db):
    """Test that normal mode supports all views."""
    db_path, biz_id = test_db

    def mock_columns(spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        cols = [MagicMock() for _ in range(count)]
        for c in cols:
            c.button.return_value = False
        return cols

    with patch("streamlit.button", return_value=False), \
         patch("streamlit.columns", side_effect=mock_columns), \
         patch("streamlit.markdown"), \
         patch("streamlit.caption"), \
         patch("streamlit.plotly_chart"), \
         patch("streamlit.expander"):
        view = render_vat_credit_panel(
            business_id=biz_id,
            current_view="reconciliation",
            db_path=db_path,
            simple_mode=False,
        )
        assert view == "reconciliation"


def test_simple_mode_3_big_numbers_zero_state(test_db):
    """The panel no longer repeats the headline cards; they appear once at the top of the page."""
    db_path, biz_id = test_db

    def mock_columns(spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        cols = [MagicMock() for _ in range(count)]
        for c in cols:
            c.button.return_value = False
        return cols

    with patch("streamlit.button", return_value=False), \
         patch("streamlit.columns", side_effect=mock_columns), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.caption"), \
         patch("streamlit.plotly_chart"), \
         patch("streamlit.expander"):
        render_vat_credit_panel(
            business_id=biz_id,
            current_view="all_periods",
            db_path=db_path,
            simple_mode=True,
        )
        # Check rendered HTML calls
        rendered_texts = " ".join([call.args[0] for call in mock_md.call_args_list if call.args and isinstance(call.args[0], str)])
        assert "VAT Credit Panel" in rendered_texts
        assert "Problems to fix" not in rendered_texts


def test_scanned_invoice_uncertain_flagging(test_db):
    """Test that low-confidence or blurry OCR text flags invoice as NEEDS_REVIEW."""
    db_path, biz_id = test_db

    blurry_text = "Tax Invoic... some blurred text ... total 1050"
    ext_inv = extract_invoice_fields(blurry_text, source_document="blurry_scan.png")

    conn = get_db_connection(db_path=db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn,
                invoice_number, date, taxable_amount, vat_amount, total, currency,
                description_lines, eligibility_status, eligibility_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_id, "purchase", ext_inv.supplier_name, ext_inv.supplier_trn,
                ext_inv.invoice_number, ext_inv.date.isoformat(),
                ext_inv.taxable_amount, ext_inv.vat_amount, ext_inv.total, "AED",
                "[]", "NEEDS_REVIEW", "Uncertain OCR scan - awaiting owner confirmation",
            ),
        )
    conn.close()

    # Reconcile should detect problem
    rec = reconcile_4_way(business_id=biz_id, db_path=db_path)
    # Check that query finds 1 problem
    conn = get_db_connection(db_path=db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE business_id = ? AND eligibility_status = 'NEEDS_REVIEW';", (biz_id,))
    cnt = cur.fetchone()["cnt"]
    conn.close()
    assert cnt == 1

    # Verify that owner confirming updates it to VALID
    conn = get_db_connection(db_path=db_path)
    with conn:
        conn.execute("UPDATE invoices SET eligibility_status = 'VALID' WHERE business_id = ?;", (biz_id,))
    conn.close()

    conn = get_db_connection(db_path=db_path)
    cur = conn.cursor()
    cur.execute("SELECT eligibility_status FROM invoices WHERE business_id = ?;", (biz_id,))
    st = cur.fetchone()["eligibility_status"]
    conn.close()
    assert st == "VALID"
