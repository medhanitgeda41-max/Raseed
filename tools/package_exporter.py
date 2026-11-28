"""Export module for generating official FTA VAT refund dossiers in Excel and PDF formats."""
import io
import json
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any, List
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from database.connection import get_db_connection, get_or_create_default_business


def generate_refund_excel_package(
    business_id: int,
    period_codes: List[str],
    claimed_amount: float,
    db_path: Optional[Path] = None,
) -> bytes:
    """Generate an official Excel workbook (.xlsx) containing the complete refund dossier."""
    conn = get_db_connection(db_path=db_path, readonly=True)
    wb = Workbook()

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM business_profile WHERE business_id = ? LIMIT 1;", (business_id,))
        prof = cursor.fetchone()
        legal_name = prof["legal_name"] if prof else "Taxable Person"
        trn = prof["trn"] if prof else "100XXXXXXXXXXXX"
        signatory = prof["signatory_name"] if prof else "Authorized Signatory"
        iban = prof["masked_iban"] if prof else "AE****************"

        # -------------------------------------------------------------
        # Sheet 1: Dossier Summary
        # -------------------------------------------------------------
        ws_sum = wb.active
        ws_sum.title = "Dossier Summary"
        ws_sum.views.sheetView[0].showGridLines = True

        title_font = Font(name="Calibri", size=16, bold=True, color="1F497D")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        cell_font = Font(name="Calibri", size=11)

        ws_sum["A1"] = "UAE FEDERAL TAX AUTHORITY — FORM 201 VAT REFUND DOSSIER"
        ws_sum["A1"].font = title_font

        summary_rows = [
            ("Legal Business Name:", legal_name),
            ("Tax Registration Number (TRN):", trn),
            ("Authorized Signatory:", signatory),
            ("Registered Bank IBAN:", iban),
            ("Claimed Refund Amount (AED):", f"{claimed_amount:,.2f}"),
            ("Tax Periods Included:", ", ".join(period_codes)),
            ("Statutory Law Authority:", "Federal Decree-Law No. 8 of 2017 & DL 16-17/2025"),
            ("Dossier Generated At:", str(date.today())),
        ]

        for idx, (label, val) in enumerate(summary_rows, 3):
            ws_sum[f"A{idx}"] = label
            ws_sum[f"A{idx}"].font = Font(bold=True)
            ws_sum[f"B{idx}"] = val
            ws_sum[f"B{idx}"].font = cell_font

        # -------------------------------------------------------------
        # Sheet 2: Supporting Tax Invoices
        # -------------------------------------------------------------
        ws_inv = wb.create_sheet(title="Supporting Tax Invoices")
        ws_inv.views.sheetView[0].showGridLines = True

        headers = ["Invoice Number", "Date", "Supplier Name", "Supplier TRN", "Taxable Amount (AED)", "VAT Amount (AED)", "Total (AED)", "Eligibility Status", "Statutory Rule"]
        ws_inv.append(headers)

        for col_num in range(1, len(headers) + 1):
            cell = ws_inv.cell(row=1, column=col_num)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        cursor.execute(
            """
            SELECT invoice_number, date, supplier_name, supplier_trn, taxable_amount, vat_amount, total, eligibility_status, eligibility_reason
            FROM invoices
            WHERE business_id = ? AND direction = 'purchase' AND eligibility_status = 'VALID'
            ORDER BY date ASC;
            """,
            (business_id,),
        )
        invoices = cursor.fetchall()
        for inv in invoices:
            ws_inv.append([
                inv["invoice_number"],
                inv["date"],
                inv["supplier_name"],
                inv["supplier_trn"],
                float(inv["taxable_amount"]),
                float(inv["vat_amount"]),
                float(inv["total"]),
                inv["eligibility_status"],
                "Executive Regulation Art. 54",
            ])

        # Adjust column widths
        for ws in (ws_sum, ws_inv):
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                col_letter = col[0].column_letter
                ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

        output = io.BytesIO()
        wb.save(output)
        return output.getvalue()
    finally:
        conn.close()


def generate_refund_pdf_package(
    business_id: int,
    period_codes: List[str],
    claimed_amount: float,
    db_path: Optional[Path] = None,
) -> bytes:
    """Generate a formal PDF VAT refund application dossier."""
    conn = get_db_connection(db_path=db_path, readonly=True)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM business_profile WHERE business_id = ? LIMIT 1;", (business_id,))
        prof = cursor.fetchone()
        legal_name = prof["legal_name"] if prof else "Taxable Person"
        trn = prof["trn"] if prof else "100XXXXXXXXXXXX"
        signatory = prof["signatory_name"] if prof else "Authorized Signatory"
        iban = prof["masked_iban"] if prof else "AE****************"

        story = []

        # Title
        title_style = ParagraphStyle(
            name="TitleStyle",
            parent=styles["Heading1"],
            fontSize=16,
            textColor=colors.HexColor("#1F497D"),
            spaceAfter=12,
        )
        story.append(Paragraph("UAE FEDERAL TAX AUTHORITY — VAT REFUND APPLICATION", title_style))
        story.append(Paragraph(f"<b>Dossier Ref:</b> REF-{business_id}-{'-'.join(period_codes)} | <b>Date:</b> {date.today()}", styles["Normal"]))
        story.append(Spacer(1, 14))

        # Business & Claim Summary Table
        summary_data = [
            ["Taxable Person:", legal_name, "Tax Registration No:", trn],
            ["Claimed Amount:", f"{claimed_amount:,.2f} AED", "Review Periods:", ", ".join(period_codes)],
            ["Signatory:", signatory, "Designated Bank IBAN:", iban],
            ["Statutory Source:", "Decree-Law 8/2017 & DL 16-17/2025", "Verification:", "100% Tax Invoice Substantiated"],
        ]
        t_summary = Table(summary_data, colWidths=[110, 160, 110, 160])
        t_summary.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F2F4F7")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#101828")),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
        ]))
        story.append(t_summary)
        story.append(Spacer(1, 16))

        # Supporting Invoices Section
        story.append(Paragraph("<b>Schedule of Verified Supporting Tax Invoices</b>", styles["Heading3"]))
        story.append(Spacer(1, 6))

        invoices = _claim_details(business_id, period_codes, db_path=db_path)["invoices"]

        inv_data = [["Invoice #", "Date", "Supplier", "Taxable (AED)", "VAT (AED)"]]
        for inv in invoices:
            inv_data.append([
                inv["invoice_number"],
                inv["date"],
                inv["supplier_name"][:25],
                f"{float(inv['taxable_amount']):,.2f}",
                f"{float(inv['vat_amount']):,.2f}",
            ])

        t_invoices = Table(inv_data, colWidths=[90, 75, 175, 100, 100])
        t_invoices.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F497D")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E4E7EC")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(t_invoices)
        story.append(Spacer(1, 20))

        # Legal Attestation
        attestation = (
            "<b>Statutory Declaration:</b> I declare that all input tax claimed in this refund package "
            "satisfies Articles 53-58 and Article 74 of UAE Federal Decree-Law No. 8 of 2017 as amended. "
            "All invoices are held in original valid format and available for FTA audit."
        )
        story.append(Paragraph(attestation, styles["Normal"]))
        story.append(Spacer(1, 30))

        # Signature Box
        sig_data = [
            [f"Authorized Signatory: {signatory}", "EmaraTax Reference: _________________________"],
            [f"Date: {date.today()}", "Official Stamp: [               ]"],
        ]
        t_sig = Table(sig_data, colWidths=[270, 270])
        t_sig.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t_sig)

        doc.build(story)
        return buffer.getvalue()
    finally:
        conn.close()


def _claim_details(business_id: int, period_codes: List[str], db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Profile, per-period figures and the supporting invoices for exactly the claimed periods."""
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM business_profile WHERE business_id = ? LIMIT 1;", (business_id,))
        prof = cur.fetchone()
        periods = []
        invoices = []
        for code in period_codes:
            cur.execute(
                "SELECT period, period_start, period_end, output_tax, input_tax FROM vat_returns "
                "WHERE business_id = ? AND period = ?;",
                (business_id, code),
            )
            ret = cur.fetchone()
            cur.execute(
                "SELECT verified_input_tax_aed, net_excess_aed, expiry_date, days_remaining, status "
                "FROM credit_ledger WHERE business_id = ? AND period_code = ?;",
                (business_id, code),
            )
            led = cur.fetchone()
            periods.append({
                "period": code,
                "start": ret["period_start"] if ret else "",
                "end": ret["period_end"] if ret else "",
                "output": float(ret["output_tax"]) if ret else 0.0,
                "input_declared": float(ret["input_tax"]) if ret else 0.0,
                "input_verified": float(led["verified_input_tax_aed"]) if led else 0.0,
                "credit": float(led["net_excess_aed"]) if led else 0.0,
                "expiry": str(led["expiry_date"]) if led else "",
                "days": int(led["days_remaining"]) if led and led["days_remaining"] is not None else None,
            })
            if ret:
                cur.execute(
                    "SELECT invoice_number, date, supplier_name, supplier_trn, taxable_amount, vat_amount FROM invoices "
                    "WHERE business_id = ? AND direction = 'purchase' AND eligibility_status = 'VALID' "
                    "AND date >= ? AND date <= ? ORDER BY date ASC;",
                    (business_id, ret["period_start"], ret["period_end"]),
                )
                for inv in cur.fetchall():
                    d = dict(inv)
                    d["period"] = code
                    invoices.append(d)
        return {"profile": dict(prof) if prof else {}, "periods": periods, "invoices": invoices}
    finally:
        conn.close()


def generate_refund_docx_package(
    business_id: int,
    period_codes: List[str],
    claimed_amount: float,
    db_path: Optional[Path] = None,
) -> bytes:
    """Generate an editable Word (.docx) VAT refund application dossier."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    data = _claim_details(business_id, period_codes, db_path=db_path)
    prof = data["profile"]
    legal_name = prof.get("legal_name") or "Taxable Person"
    trn = prof.get("trn") or "100XXXXXXXXXXXX"
    signatory = prof.get("signatory_name") or "Authorised Signatory"
    iban = prof.get("masked_iban") or "AE**************"
    navy = RGBColor(0x1F, 0x49, 0x7D)

    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2)
        s.top_margin = s.bottom_margin = Cm(1.8)
    base = doc.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(11)
    for lvl, size in (("Heading 1", 16), ("Heading 2", 13)):
        st_ = doc.styles[lvl]
        st_.font.name = "Calibri"
        st_.font.size = Pt(size)
        st_.font.color.rgb = navy

    def shade(cell, hex_fill: str) -> None:
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_fill)
        tcPr.append(shd)

    def table(rows: List[List[str]], header: bool = True, widths: Optional[List[float]] = None):
        t = doc.add_table(rows=len(rows), cols=len(rows[0]))
        t.style = "Table Grid"
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                cell = t.cell(r, c)
                cell.text = str(val)
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(9.5)
                        if header and r == 0:
                            run.font.bold = True
                            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                if header and r == 0:
                    shade(cell, "1F497D")
                if widths:
                    cell.width = Cm(widths[c])
        return t

    doc.add_heading("VAT Refund Application Dossier", level=1)
    p = doc.add_paragraph()
    p.add_run("Prepared for submission on the FTA EmaraTax portal. ").italic = True
    p.add_run(f"Dossier ref: REF-{business_id}-{'-'.join(period_codes)} · Date: {date.today().isoformat()}")

    doc.add_heading("1. Taxable person", level=2)
    table([
        ["Legal name", legal_name],
        ["Tax Registration Number (TRN)", trn],
        ["Authorised signatory", signatory],
        ["Refund bank account (IBAN)", iban],
    ], header=False, widths=[6, 11])

    doc.add_heading("2. Refund claimed", level=2)
    para = doc.add_paragraph()
    run = para.add_run(f"Total refund requested: AED {claimed_amount:,.2f}")
    run.bold = True
    run.font.size = Pt(13)
    rows = [["Tax period", "Dates", "Output VAT", "Input VAT (return)", "Input VAT (verified)", "Excess credit", "Claim deadline"]]
    for pr in data["periods"]:
        dl = pr["expiry"] + (f" ({pr['days']} days left)" if pr["days"] is not None else "")
        rows.append([
            pr["period"], f"{pr['start']} to {pr['end']}", f"{pr['output']:,.2f}", f"{pr['input_declared']:,.2f}",
            f"{pr['input_verified']:,.2f}", f"{pr['credit']:,.2f}", dl,
        ])
    rows.append(["Total", "", "", "", "", f"{sum(pr['credit'] for pr in data['periods']):,.2f}", ""])
    table(rows)
    doc.add_paragraph(
        "Excess credit = input VAT verified against valid tax invoices minus output VAT, for each tax period. "
        "All amounts in AED."
    ).runs[0].font.size = Pt(9)

    doc.add_heading("3. Supporting tax invoices", level=2)
    if data["invoices"]:
        inv_rows = [["Period", "Invoice no.", "Date", "Supplier", "Supplier TRN", "Taxable (AED)", "VAT (AED)"]]
        for inv in data["invoices"]:
            inv_rows.append([
                inv["period"], inv["invoice_number"], str(inv["date"])[:10], str(inv["supplier_name"] or "")[:30],
                inv["supplier_trn"] or "", f"{float(inv['taxable_amount']):,.2f}", f"{float(inv['vat_amount']):,.2f}",
            ])
        table(inv_rows)
        doc.add_paragraph(f"{len(data['invoices'])} invoice(s). Originals are held by the taxable person and available for FTA review.").runs[0].font.size = Pt(9)
    else:
        doc.add_paragraph("No individual invoices on file for these periods; the claim relies on the filed VAT returns.")

    doc.add_heading("4. EmaraTax submission checklist", level=2)
    for item in (
        "Log in to EmaraTax and open VAT > VAT Refund (Form VAT311).",
        "Enter the refund amount shown in section 2 and the tax periods it covers.",
        "Attach this dossier and the supporting invoices listed in section 3.",
        "Confirm the bank account details (IBAN) for the refund payment.",
        "Submit, then record the EmaraTax reference number in Raseed so the claim is tracked.",
    ):
        doc.add_paragraph(item, style="List Number")

    doc.add_heading("5. Declaration", level=2)
    doc.add_paragraph(
        "I declare that the input tax claimed in this application relates to taxable supplies made by the business, "
        "is supported by valid tax invoices held in original form, and has not been claimed before, in line with "
        "Federal Decree-Law No. 8 of 2017 on Value Added Tax and its Executive Regulation."
    )
    sig = doc.add_table(rows=2, cols=2)
    sig.cell(0, 0).text = f"Authorised signatory: {signatory}"
    sig.cell(0, 1).text = "Signature: ______________________"
    sig.cell(1, 0).text = f"Date: {date.today().isoformat()}"
    sig.cell(1, 1).text = "EmaraTax reference: ______________"

    foot = doc.sections[0].footer.paragraphs[0]
    foot.text = "Prepared with Raseed. All amounts calculated by rule-based code; please review before submission."
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in foot.runs:
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
