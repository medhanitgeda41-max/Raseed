"""VAT 201 returns in the layouts people actually upload."""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

from ingest.doc_classifier import classify_document, classify_text
from ingest.returns_ledger_parser import parse_vat_returns, _period_dates


def _summary_table_pdf() -> bytes:
    buf = io.BytesIO()
    rows = [["Period", "Output VAT", "Input VAT", "Net position", "Status"],
            ["Q1 2026", "42000", "51000", "-9000", "Filed - demo"],
            ["Q2 2026", "46000", "57000", "-11000", "Filed - demo"],
            ["Q3 2026", "39000", "48000", "-9000", "Filed - demo"],
            ["Q4 2026", "44000", "52000", "-8000", "Draft - demo"]]
    SimpleDocTemplate(buf, pagesize=A4).build(
        [Paragraph("VAT201 Return Summaries (Demo)", getSampleStyleSheet()["Title"]), Table(rows)])
    return buf.getvalue()


def _official_style_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in ["VAT Return - Form VAT201", "TRN: 100234567800003", "Tax Period: 01/01/2026 - 31/03/2026",
                 "VAT on Sales and all other Outputs", "Total value of due tax for the period: 12,500.00",
                 "VAT on Expenses and all other Inputs", "Total value of recoverable tax for the period: 20,000.00",
                 "Date of Submission: 28/04/2026"]:
        c.drawString(50, y, line); y -= 22
    c.save()
    return buf.getvalue()


def test_summary_table_pdf_is_a_return_and_reads_filed_periods_only():
    b = _summary_table_pdf()
    assert classify_document(b, "02_Demo_VAT201_Return_Summaries.pdf")["type"] == "vat_return"
    rows = parse_vat_returns(b, "02_Demo_VAT201_Return_Summaries.pdf")
    assert [r["period"] for r in rows] == ["2026-Q1", "2026-Q2", "2026-Q3"]  # Q4 is a draft
    assert rows[1]["input_tax"] == 57000 and rows[1]["output_tax"] == 46000
    assert rows[0]["period_end"] == "2026-03-31"


def test_official_fta_wording_is_read():
    b = _official_style_pdf()
    assert classify_document(b, "form.pdf")["type"] == "vat_return"
    (r,) = parse_vat_returns(b, "form.pdf")
    assert (r["period"], r["output_tax"], r["input_tax"], r["filing_date"]) == ("2026-Q1", 12500.0, 20000.0, "2026-04-28")


def test_period_formats():
    assert _period_dates("Q1 2026")[0] == "2026-Q1"
    assert _period_dates("2025-Q4")[1:] == ("2025-10-01", "2025-12-31")
    assert _period_dates("Q32026")[0] == "2026-Q3"
    assert _period_dates("Jan-Mar 2026")[0] == "2026-Q1"


def test_lecture_about_vat_returns_is_still_not_a_return():
    notes = ("Lecture 7: Accounting for VAT. Students should understand output tax and input tax. "
             "A VAT return is filed quarterly. Homework: read chapter 4.")
    assert classify_text(notes)["type"] == "other"


def test_customs_evidence_is_not_an_invoice_and_gives_import_vat():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in ["Import Customs VAT Evidence", "Customs Declaration No: 101-2026-0001234", "Date: 15/02/2026",
                 "Customs value: 120,000.00 AED", "Import VAT (5%): 6,000.00 AED"]:
        c.drawString(50, y, line); y -= 22
    c.save()
    b = buf.getvalue()
    assert classify_document(b, "customs.pdf")["type"] == "evidence"
    from ingest.document_repair import parse_customs_import_vat
    imp = parse_customs_import_vat(b, "customs.pdf")
    assert imp["vat_amount"] == 6000.0 and imp["date"] == "2026-02-15"


def test_credit_balance_statement_is_evidence():
    text = ("VAT Credit Balance Statement\nPeriod Opening balance Excess credit Closing balance\n"
            "Q1 2026 0.00 9,000.00 9,000.00\nCredit carried forward: 9,000.00")
    assert classify_text(text)["type"] == "evidence"


def test_offline_answer_is_a_real_answer(tmp_path):
    from agent.chat_agent import offline_answer
    from database.connection import init_db
    from auth.company_auth import register_company_account
    db = tmp_path / "t.db"
    init_db(db_path=db)
    txt = offline_answer(1, "how much can I get back?", db_path=db)
    assert "AED" in txt or "refund" in txt.lower()
    assert not txt.startswith("[AI unavailable")
