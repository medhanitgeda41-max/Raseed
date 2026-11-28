"""File analysis v2: invoice reader, multi-sheet registers, cross-file duplicates, auto-repair.

Built around a real user's upload: 6 purchase + 3 sales tax invoices (2026, DD/MM/YYYY dates),
a VAT return summary, an Input/Output VAT workbook listing the same invoices, and evidence files.
The correct refund is AED 29,000 (Q1 9,000 + Q2 11,000 + Q3 9,000).
"""
import io
from datetime import date

import openpyxl
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table
from reportlab.lib.styles import getSampleStyleSheet

from ingest.invoice_reader import parse_date, read_invoice, to_number

OWN, OWN_TRN = "Al Noor Trading LLC", "100123456789003"
PURCH = [("PUR-2601", "14/01/2026", "Gulf Office Supplies LLC", "100555666777003", 255000.0),
         ("PUR-2602", "18/03/2026", "Emirates Logistics FZE", "100444333222003", 765000.0),
         ("PUR-2603", "09/04/2026", "Desert IT Solutions LLC", "100888999000003", 600000.0),
         ("PUR-2604", "22/06/2026", "Gulf Office Supplies LLC", "100555666777003", 540000.0),
         ("PUR-2605", "03/08/2026", "Emirates Logistics FZE", "100444333222003", 480000.0),
         ("PUR-2606", "15/09/2026", "Desert IT Solutions LLC", "100888999000003", 480000.0)]
SALES = [("SAL-2601", "28/02/2026", "Marina Retail Group LLC", "100777111222003", 840000.0),
         ("SAL-2602", "30/05/2026", "Marina Retail Group LLC", "100777111222003", 920000.0),
         ("SAL-2603", "25/08/2026", "Oasis Hypermarkets LLC", "100333444555003", 780000.0)]


def _invoice_pdf(number, d, seller, seller_trn, buyer, buyer_trn, net):
    ss = getSampleStyleSheet()
    buf = io.BytesIO()
    vat = round(net * 0.05, 2)
    rows = [["Description", "Amount (AED)"], ["Goods / services", f"{net:,.2f}"],
            ["Subtotal (excl. VAT)", f"{net:,.2f}"], ["VAT 5%", f"{vat:,.2f}"], ["Total (incl. VAT)", f"{net + vat:,.2f}"]]
    SimpleDocTemplate(buf, pagesize=A4).build([
        Paragraph("TAX INVOICE", ss["Title"]),
        Paragraph(f"<b>{seller}</b><br/>TRN: {seller_trn}", ss["Normal"]), Spacer(1, 6),
        Paragraph(f"Invoice No: {number}<br/>Invoice Date: {d}<br/>Bill To: {buyer}<br/>Customer TRN: {buyer_trn}", ss["Normal"]),
        Table(rows)])
    return buf.getvalue()


def _files():
    files = []
    for n, d, s, trn, net in PURCH:
        files.append((f"Purchase_Invoice_{n}.pdf", _invoice_pdf(n, d, s, trn, OWN, OWN_TRN, net)))
    for n, d, b, trn, net in SALES:
        files.append((f"Sales_Invoice_{n}.pdf", _invoice_pdf(n, d, OWN, OWN_TRN, b, trn, net)))
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Return Summary"
    ws.append(["Tax Period", "Output VAT (AED)", "Input VAT (AED)", "Status"])
    for r in [("Q1 2026", 42000, 51000, "Filed"), ("Q2 2026", 46000, 57000, "Filed"),
              ("Q3 2026", 39000, 48000, "Filed"), ("Q4 2026", 44000, 52000, "Draft")]:
        ws.append(list(r))
    b = io.BytesIO(); wb.save(b); files.append(("02_Return_Summary.xlsx", b.getvalue()))
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Input VAT"
    ws.append(["Input / Output VAT report"]); ws.append([])
    ws.append(["Invoice No", "Supplier", "Supplier TRN", "Invoice Date", "Net Amount", "VAT Amount"])
    for n, d, s, trn, net in PURCH:
        ws.append([n, s, trn, d, net, round(net * .05, 2)])
    ws.append(["Total", "", "", "", sum(p[4] for p in PURCH), sum(round(p[4] * .05, 2) for p in PURCH)])
    ws2 = wb.create_sheet("Output VAT")
    ws2.append(["Invoice No", "Customer", "Customer TRN", "Invoice Date", "Net Amount", "VAT Amount"])
    for n, d, c, trn, net in SALES:
        ws2.append([n, c, trn, d, net, round(net * .05, 2)])
    b = io.BytesIO(); wb.save(b); files.append(("03_Input_Output_VAT.xlsx", b.getvalue()))
    return files


# ------------------------------------------------------------------ unit level

@pytest.mark.parametrize("text,iso", [
    ("Invoice Date: 14/01/2026", "2026-01-14"), ("Date 2026-03-18", "2026-03-18"), ("dated 9.4.2026", "2026-04-09"),
    ("Date: 22-Jun-2026", "2026-06-22"), ("Issued March 3, 2026", "2026-03-03"), ("12 January 2026", "2026-01-12"),
])
def test_dates(text, iso):
    assert parse_date(text) == iso


def test_ocr_number_formats():
    assert to_number("1.250.00") == 1250.0      # phone OCR turns the comma into a dot
    assert to_number("26,250.00") == 26250.0
    assert to_number("(7,500.00)") == -7500.0


def test_reader_sale_vs_purchase_and_fields():
    from ingest.doc_classifier import document_text
    p = document_text(_invoice_pdf("PUR-2601", "14/01/2026", "Gulf Office Supplies LLC", "100555666777003",
                                   OWN, OWN_TRN, 255000.0), "p.pdf")["text"]
    r = read_invoice(p, "p.pdf", own_trn=OWN_TRN, own_name=OWN + " (Demo)")
    assert (r["direction"], r["invoice_number"], r["date"], r["supplier_trn"]) == ("purchase", "PUR-2601", "2026-01-14", "100555666777003")
    assert (r["taxable_amount"], r["vat_amount"], r["total"]) == (255000.0, 12750.0, 267750.0)
    s = document_text(_invoice_pdf("SAL-2601", "28/02/2026", OWN, OWN_TRN, "Marina Retail Group LLC",
                                   "100777111222003", 840000.0), "s.pdf")["text"]
    assert read_invoice(s, "s.pdf", own_trn=OWN_TRN, own_name=OWN)["direction"] == "sales"


def test_workbook_with_title_rows_and_two_sheets():
    from ingest.doc_classifier import classify_document
    from ingest.excel_parser import parse_excel_register
    name, data = _files()[-1]
    assert classify_document(data, name)["type"] == "register"
    recs, _ = parse_excel_register(data, name)
    assert len(recs) == 9                                   # the 'Total' row is not an invoice
    assert sum(r["direction"] == "sales" for r in recs) == 3
    assert recs[0]["date"] == "2026-01-14"


# ------------------------------------------------------------------ end to end

def _run(tmp_path, monkeypatch, corrupt_then_repair=False):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "up"))
    from database.connection import init_db, get_db_connection
    from auth.company_auth import register_company_account
    from ingest.doc_classifier import classify_document
    from ingest.file_storage import store_uploaded_file
    from ingest.document_repair import extract_into_db, repair_document_types
    from tools.compute_tools import rebuild_credit_ledger, declared_vs_proven, document_problems
    db = tmp_path / "e2e.db"
    init_db(db_path=db)
    register_company_account(company_name=OWN, trn=OWN_TRN, owner_name="O", email="o@x.ae",
                             password="Passw0rd!Strong", db_path=db)
    conn = get_db_connection(db_path=db)
    biz = conn.execute("SELECT id FROM businesses WHERE trn = ?", (OWN_TRN,)).fetchone()["id"]
    conn.close()
    for name, data in _files():
        c = classify_document(data, name)
        assert c["type"] != "other", name
        _, _, _, doc_id = store_uploaded_file(data, name, c["type"], business_id=biz, db_path=db, upload_dir=tmp_path / "up")
        if c.get("text"):
            conn = get_db_connection(db_path=db)
            conn.execute("UPDATE uploaded_documents SET extracted_text = ? WHERE id = ?", (c["text"], doc_id)); conn.commit(); conn.close()
        extract_into_db(biz, doc_id, c["type"], data, name, db_path=db)
    if corrupt_then_repair:  # what the old reader stored: wrong dates, sales counted as purchases
        conn = get_db_connection(db_path=db)
        conn.execute("UPDATE uploaded_documents SET reader_version = 0 WHERE business_id = ?", (biz,))
        conn.execute("UPDATE invoices SET date = '2024-01-15', direction = 'purchase' WHERE business_id = ?", (biz,))
        conn.commit(); conn.close()
        rebuild_credit_ledger(business_id=biz, db_path=db, current_date=date(2026, 11, 28))
        assert declared_vs_proven(biz, db_path=db)["proven"] == 0
        repair_document_types(biz, db_path=db)
        assert repair_document_types(biz, db_path=db) == []        # nothing re-read on the next load
    rebuild_credit_ledger(business_id=biz, db_path=db, current_date=date(2026, 11, 28))
    return biz, db, declared_vs_proven(biz, db_path=db), document_problems(biz, db_path=db)


def test_user_upload_gives_the_right_refund_and_no_false_problems(tmp_path, monkeypatch):
    biz, db, dv, probs = _run(tmp_path, monkeypatch)
    assert dv == {"declared": 29000.0, "proven": 29000.0, "gap": 0.0, "unproven_periods": []}
    assert probs == {}
    from database.connection import get_db_connection
    conn = get_db_connection(db_path=db)
    dups = conn.execute("SELECT COUNT(*) n FROM invoices WHERE business_id = ? AND eligibility_status = 'DUPLICATE'", (biz,)).fetchone()["n"]
    conn.close()
    assert dups == 9  # every invoice listed in the workbook is also a PDF: counted once


def test_old_wrong_data_is_repaired_automatically(tmp_path, monkeypatch):
    _biz, _db, dv, probs = _run(tmp_path, monkeypatch, corrupt_then_repair=True)
    assert dv["proven"] == 29000.0 and probs == {}


def test_real_problems_are_still_found(tmp_path, monkeypatch):
    biz, db, _dv, _p = _run(tmp_path, monkeypatch)
    from database.connection import get_db_connection
    from tools.compute_tools import document_problems
    conn = get_db_connection(db_path=db)
    conn.execute("UPDATE invoices SET vat_amount = 13000 WHERE invoice_number = 'PUR-2601' AND eligibility_status = 'VALID'")
    conn.commit(); conn.close()
    msgs = [m for v in document_problems(biz, db_path=db).values() for m in v["problems"]]
    assert any("PUR-2601" in m and "not 5%" in m for m in msgs)
