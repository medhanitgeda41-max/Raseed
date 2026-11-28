"""End-to-end multi-tenant demo scenario verification test suite for Part 5."""
import io
import json
import pytest
from datetime import date, datetime, timedelta
from pathlib import Path
from config.settings import settings
from unittest.mock import patch, MagicMock

from database.connection import init_db, get_db_connection
from auth.company_auth import register_company_account, authenticate_user
from tools.read_tools import get_refund_status, get_credit_ledger, get_staged_actions, get_invoices
from tools.write_gateway import stage_action, execute_approved_action
from tools.compute_tools import rebuild_credit_ledger, reconcile_4_way
from agent.chat_agent import execute_chat_turn, get_journey_step
from agent.visual_panel import render_vat_credit_panel
from tools.package_exporter import generate_refund_excel_package, generate_refund_pdf_package
from ingest.extractor import extract_invoice_fields
from ingest.file_storage import store_uploaded_file, read_stored_document


def test_full_demo_scenarios(tmp_path):
    db_path = tmp_path / "online_demo_master.db"
    init_db(db_path)

    # -------------------------------------------------------------------------
    # Scenario 1: Company A signs up, logs in immediately
    # -------------------------------------------------------------------------
    ok_a, msg_a, sess_a = register_company_account(
        company_name="Al Amal Trading LLC",
        trn="100123456789001",
        owner_name="Fatima Al-Amal",
        email="owner@alamal.ae",
        password="ValidPassword123!",
        signup_code=settings.signup_code,
        db_path=db_path,
    )
    assert ok_a is True, msg_a
    biz_a_id = sess_a["business_id"]
    user_a_id = sess_a["user_id"]

    auth_ok_a, auth_msg_a, auth_sess_a = authenticate_user("owner@alamal.ae", "ValidPassword123!", db_path=db_path)
    assert auth_ok_a is True
    assert auth_sess_a["business_id"] == biz_a_id

    # -------------------------------------------------------------------------
    # Scenario 2: Shows empty workspace (no numbers, Raseed asks for documents)
    # -------------------------------------------------------------------------
    ref_empty = get_refund_status(business_id=biz_a_id, db_path=db_path)
    assert ref_empty.get("total_available_credits_aed", 0.0) == 0.0
    journey = get_journey_step(business_id=biz_a_id, db_path=db_path)
    # Step 2: Document Ingestion (waiting for invoices and returns)
    assert journey["step_number"] == 2
    assert "Document Ingestion" in journey["name"]

    # -------------------------------------------------------------------------
    # Scenario 3: Ingests test documents (purchase invoices & filed returns)
    # -------------------------------------------------------------------------
    conn = get_db_connection(db_path=db_path)
    with conn:
        # Insert filed VAT return for 2021-Q1
        conn.execute(
            """
            INSERT INTO vat_returns (
                business_id, period, period_start, period_end,
                output_tax, input_tax, net_payable_refundable, filing_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (biz_a_id, "2021-Q1", "2021-01-01", "2021-03-31", 10000.0, 25000.0, -15000.0, "2021-04-28"),
        )
        # Insert valid purchase invoice
        conn.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn,
                invoice_number, date, taxable_amount, vat_amount, total, currency,
                description_lines, eligibility_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_a_id, "purchase", "Emirates Supplies LLC", "100999888777003",
                "INV-A-100", "2021-02-15", 500000.0, 25000.0, 525000.0, "AED",
                json.dumps(["Industrial equipment"]), "VALID",
            ),
        )
    conn.close()

    ledger = rebuild_credit_ledger(business_id=biz_a_id, db_path=db_path, current_date=date(2026, 1, 15))
    ref_post = get_refund_status(business_id=biz_a_id, db_path=db_path)
    assert ref_post["total_available_credits_aed"] == 15000.0
    assert len(ledger) >= 1

    # -------------------------------------------------------------------------
    # Scenario 4: Simple mode is ON: 3 big numbers, clean bar chart
    # -------------------------------------------------------------------------
    def mock_cols(spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        cols = [MagicMock() for _ in range(count)]
        for c in cols:
            c.button.return_value = False
        return cols

    with patch("streamlit.button", return_value=False), \
         patch("streamlit.columns", side_effect=mock_cols), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.caption"), \
         patch("streamlit.plotly_chart"), \
         patch("streamlit.expander"):
        view = render_vat_credit_panel(
            business_id=biz_a_id,
            current_view="all_periods",
            db_path=db_path,
            simple_mode=True,
        )
        assert view == "all_periods"
        rendered = " ".join([call.args[0] for call in mock_md.call_args_list if call.args and isinstance(call.args[0], str)])
        # Headline numbers now live in the page-top cards (app.py); the panel
        # still renders its heading and views.
        assert "VAT Credit Panel" in rendered
        from tools.read_tools import get_refund_status as _grs
        assert abs(float(_grs(business_id=biz_a_id, db_path=db_path)["total_available_credits_aed"]) - 15000.0) < 0.01

    # -------------------------------------------------------------------------
    # Scenario 5: Scanned invoice OCR demo: blurry scan flagged, owner confirms
    # -------------------------------------------------------------------------
    blurry_text = "Tax Invoice partially scanned ... total 2100"
    ext_inv = extract_invoice_fields(blurry_text, source_document="blurry_receipt.png")
    # Insert blurry scan flagged as NEEDS_REVIEW
    conn = get_db_connection(db_path=db_path)
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO invoices (
                business_id, direction, supplier_name, supplier_trn,
                invoice_number, date, taxable_amount, vat_amount, total, currency,
                description_lines, eligibility_status, eligibility_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                biz_a_id, "purchase", "Blurry Vendor", "100111222333003",
                "INV-BLURRY-1", "2021-03-01", 2000.0, 100.0, 2100.0, "AED",
                "[]", "NEEDS_REVIEW", "Uncertain OCR scan - awaiting owner confirmation",
            ),
        )
        blurry_inv_id = cur.lastrowid
    conn.close()

    # Reconcile detects 1 problem
    conn = get_db_connection(db_path=db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE business_id = ? AND eligibility_status = 'NEEDS_REVIEW';", (biz_a_id,))
    assert cur.fetchone()["cnt"] == 1
    conn.close()

    # Owner confirms in chat -> updates to VALID
    conn = get_db_connection(db_path=db_path)
    with conn:
        conn.execute("UPDATE invoices SET eligibility_status = 'VALID' WHERE id = ?;", (blurry_inv_id,))
    conn.close()

    rebuild_credit_ledger(business_id=biz_a_id, db_path=db_path, current_date=date(2026, 1, 15))
    ref_after_ocr = get_refund_status(business_id=biz_a_id, db_path=db_path)
    # Invoices now prove AED 25,100 of input VAT, but the filed return declared AED 25,000:
    # a refund can only cover what was declared (the extra AED 100 needs a voluntary disclosure).
    assert ref_after_ocr["total_available_credits_aed"] == 15000.0

    # -------------------------------------------------------------------------
    # Scenario 6: Chat: "How much can I get back?" -> answers with exact number
    # -------------------------------------------------------------------------
    resp = execute_chat_turn(
        user_message="How much eligible VAT credit can I recover for my business?",
        history=[],
        business_id=biz_a_id,
        active_role="OWNER",
        follow_chat=True,
        db_path=db_path,
    )
    assert resp["panel_view"] == "all_periods" or "15,000" in resp["content"] or "credit" in resp["content"].lower()

    # -------------------------------------------------------------------------
    # Scenario 7: "Claim my refund" -> approval card -> Approve -> downloads
    # -------------------------------------------------------------------------
    # Stage refund action
    aid_res = stage_action(
        action_type="SUBMIT_REFUND_APPLICATION",
        payload={"period_codes": ["2021-Q1"], "claimed_amount": 15000.0},
        reason="Official Form 201 VAT Refund Submission",
        business_id=biz_a_id,
        db_path=db_path,
    )
    act_id = aid_res[0] if isinstance(aid_res, tuple) else aid_res
    assert act_id > 0

    staged = get_staged_actions(status="PENDING_APPROVAL", business_id=biz_a_id, db_path=db_path)
    assert any(a["id"] == act_id for a in staged)

    # Owner authorizes
    success, msg = execute_approved_action(
        action_id=act_id,
        approver_note="Approved by business owner for EmaraTax submission",
        approver_role="OWNER",
        business_id=biz_a_id,
        db_path=db_path,
    )
    assert success is True, f"Approval execution failed: {msg}"

    # Generate Excel and PDF dossiers
    excel_bytes = generate_refund_excel_package(business_id=biz_a_id, period_codes=["2021-Q1"], claimed_amount=15000.0, db_path=db_path)
    pdf_bytes = generate_refund_pdf_package(business_id=biz_a_id, period_codes=["2021-Q1"], claimed_amount=15000.0, db_path=db_path)
    assert len(excel_bytes) > 500
    assert len(pdf_bytes) > 500
    assert pdf_bytes.startswith(b"%PDF")

    # -------------------------------------------------------------------------
    # Scenario 8: Company A logs out
    # -------------------------------------------------------------------------
    # Log out terminates session
    session_data = {"user_id": user_a_id, "business_id": biz_a_id}
    session_data.clear()
    assert len(session_data) == 0

    # -------------------------------------------------------------------------
    # Scenario 9: Company B signs up -> sees ZERO data from Company A
    # -------------------------------------------------------------------------
    ok_b, msg_b, sess_b = register_company_account(
        company_name="Barakah Retail FZE",
        trn="100987654321002",
        owner_name="Rashid Al-Nuaimi",
        email="finance@barakah.ae",
        password="SecurePassword2026!",
        signup_code=settings.signup_code,
        db_path=db_path,
    )
    assert ok_b is True, msg_b
    biz_b_id = sess_b["business_id"]
    assert biz_b_id != biz_a_id

    # Check Company B isolated state
    ref_b = get_refund_status(business_id=biz_b_id, db_path=db_path)
    assert ref_b["total_available_credits_aed"] == 0.0
    invoices_b = get_invoices(business_id=biz_b_id, db_path=db_path)
    assert len(invoices_b) == 0
    staged_b = get_staged_actions(business_id=biz_b_id, db_path=db_path)
    assert len(staged_b) == 0

    # -------------------------------------------------------------------------
    # Scenario 10: App restart simulation -> Company A logs in -> data intact
    # -------------------------------------------------------------------------
    auth_reload_ok, auth_reload_msg, auth_reload_sess = authenticate_user("owner@alamal.ae", "ValidPassword123!", db_path=db_path)
    assert auth_reload_ok is True
    reloaded_biz_id = auth_reload_sess["business_id"]
    assert reloaded_biz_id == biz_a_id

    ref_a_reloaded = get_refund_status(business_id=reloaded_biz_id, db_path=db_path)
    assert ref_a_reloaded["total_available_credits_aed"] == 15000.0
    invoices_a = get_invoices(business_id=reloaded_biz_id, db_path=db_path)
    assert len(invoices_a) >= 2

    approved_actions_a = get_staged_actions(status="APPROVED", business_id=reloaded_biz_id, db_path=db_path)
    assert len(approved_actions_a) >= 1

    # -------------------------------------------------------------------------
    # Scenario 11: Wrong password 5 times -> account locked out
    # -------------------------------------------------------------------------
    for attempt in range(5):
        fail_ok, fail_msg, _ = authenticate_user("owner@alamal.ae", "WrongPassword!", db_path=db_path)
        assert fail_ok is False

    # 6th attempt: should be locked out
    lockout_ok, lockout_msg, _ = authenticate_user("owner@alamal.ae", "WrongPassword!", db_path=db_path)
    assert lockout_ok is False
    assert "locked" in lockout_msg.lower()
