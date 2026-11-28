"""Quick analysis: read files in memory and summarise them, without saving anything.

Used by the chat's "Quick analysis" box. Nothing is written to the database or
to cloud storage; the result only lives in the current session (lost at logout).
The same deterministic rules as the main analysis are used for the numbers.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Tuple

from guards.deadline_engine import calculate_credit_expiry
from ingest.doc_classifier import classify_document, DOC_TYPE_LABELS


def _is_credit_note(desc: Any, taxable: float) -> bool:
    d = str(desc or "").lower()
    return taxable < 0 or "credit note" in d or "credit-note" in d or "cr note" in d


def _read_files(files: List[Tuple[str, bytes]], business_id: int | None = None) -> Dict[str, Any]:
    from ingest.excel_parser import parse_excel_register
    from ingest.returns_ledger_parser import parse_vat_returns, parse_general_ledger

    invoices: List[Dict[str, Any]] = []
    returns: List[Dict[str, Any]] = []
    ledger: List[Dict[str, Any]] = []
    read_log: List[str] = []

    for name, data in files:
        cls = classify_document(data, name)
        ftype = cls["type"]
        label = DOC_TYPE_LABELS.get(ftype, ftype)
        if ftype == "other":
            read_log.append(f"`{name}` → 🚫 not a VAT document ({'; '.join(cls['reasons'][:2])}), skipped")
            continue
        try:
            if ftype == "register":
                recs, _ = parse_excel_register(file_bytes=data, filename=name, custom_mapping=None, business_id=None)
                for r in recs:
                    invoices.append({
                        "invoice_number": r.get("invoice_number"), "date": str(r.get("date")),
                        "supplier_trn": r.get("supplier_trn"), "taxable_amount": float(r.get("taxable_amount", 0) or 0),
                        "vat_amount": float(r.get("vat_amount", 0) or 0), "description": " ".join(r.get("description_lines", []) or []),
                    })
                read_log.append(f"`{name}` → {label}: {len(recs)} invoice line(s)")
            elif ftype == "vat_return":
                rows = parse_vat_returns(data, name)
                returns.extend(rows)
                read_log.append(f"`{name}` → {label}: {len(rows)} period(s)")
            elif ftype == "ledger":
                rows = parse_general_ledger(data, name)
                ledger.extend(rows)
                read_log.append(f"`{name}` → {label}: {len(rows)} line(s)")
            elif ftype == "invoice":
                from ingest.extractor import extract_invoice_fields
                if name.lower().endswith(".pdf"):
                    from ingest.pdf_parser import extract_text_from_pdf
                    text = extract_text_from_pdf(data).get("text", "")
                else:
                    from ingest.ocr_engine import extract_ocr_from_image
                    _o = extract_ocr_from_image(data); text = ((_o.get("row_text") or "") + "\n" + _o.get("text", "")).strip()
                inv = extract_invoice_fields(text, source_document=name, business_id=business_id)
                if inv.direction == "sales":
                    read_log.append(f"`{name}` → sales invoice {inv.invoice_number} (VAT you charged, not a credit)")
                    continue
                invoices.append({
                    "invoice_number": inv.invoice_number, "date": inv.date.isoformat(), "supplier_trn": inv.supplier_trn,
                    "taxable_amount": float(inv.taxable_amount), "vat_amount": float(inv.vat_amount),
                    "description": " ".join(inv.description_lines or []),
                })
                read_log.append(f"`{name}` → {label}: invoice {inv.invoice_number}, VAT AED {inv.vat_amount:,.2f}")
            else:
                read_log.append(f"`{name}` → {label}: not used for numbers")
        except Exception as exc:
            read_log.append(f"`{name}` → could not be read ({exc})")
    return {"invoices": invoices, "returns": returns, "ledger": ledger, "read_log": read_log}


def quick_analyze(files: List[Tuple[str, bytes]], today: date | None = None, business_id: int | None = None) -> Dict[str, Any]:
    """Analyse in-memory files and return {'markdown': str, 'totals': dict}."""
    today = today or date.today()
    data = _read_files(files, business_id=business_id)
    invoices, returns, ledger = data["invoices"], data["returns"], data["ledger"]

    # Basic invoice checks (same rules as the main engine)
    bad_vat = [i for i in invoices if abs(round(abs(i["taxable_amount"]) * 0.05, 2) - abs(i["vat_amount"])) > 0.05]
    bad_trn = [i for i in invoices if not (str(i.get("supplier_trn") or "").isdigit()
                                           and len(str(i.get("supplier_trn"))) == 15
                                           and str(i.get("supplier_trn")).startswith("100"))]
    seen, dups = set(), []
    for i in invoices:
        key = (str(i.get("supplier_trn")), str(i.get("invoice_number")))
        if key in seen:
            dups.append(i)
        seen.add(key)

    lines: List[str] = ["**⚡ Quick analysis (not saved)**", ""]
    lines += ["**Files read:**"] + [f"- {l}" for l in data["read_log"]] + [""]

    total_credit = at_risk = 0.0
    period_rows: List[str] = []
    for r in sorted(returns, key=lambda x: x["period_end"]):
        p_start, p_end = r["period_start"], r["period_end"]
        verified = 0.0
        for i in invoices:
            if p_start <= str(i["date"])[:10] <= p_end:
                v = abs(i["vat_amount"])
                verified += -v if _is_credit_note(i.get("description"), i["taxable_amount"]) else v
        verified = round(verified, 2)
        has_invoices = any(p_start <= str(i["date"])[:10] <= p_end for i in invoices)
        basis = verified if has_invoices else r["input_tax"]
        excess = round(max(basis - r["output_tax"], 0.0), 2)
        dl = calculate_credit_expiry(period_code=r["period"], period_end_date=date.fromisoformat(p_end), current_date=today)
        if dl.status != "EXPIRED":
            total_credit += excess
            if dl.status in ("URGENT", "DUE_SOON"):
                at_risk += excess
        gap = round(r["input_tax"] - verified, 2) if has_invoices else None
        flag = {"URGENT": "🔴 URGENT", "DUE_SOON": "🟡 Due soon", "EXPIRED": "⚫ Expired"}.get(dl.status, "🟢 OK")
        note = "" if has_invoices else " (no invoices attached; based on the return only)"
        if gap and abs(gap) > 0.05:
            note += f" · return claims AED {gap:,.2f} more than the invoices" if gap > 0 else f" · invoices exceed the return by AED {abs(gap):,.2f}"
        period_rows.append(
            f"| {r['period']} | {r['input_tax']:,.2f} | {(format(verified, ',.2f') if has_invoices else '–')} | {r['output_tax']:,.2f} | "
            f"**{excess:,.2f}** | {dl.effective_expiry_date.isoformat()} ({dl.days_remaining} days) {flag}{note} |"
        )

    if period_rows:
        lines += [
            "| Period | Input VAT on return | Input VAT on invoices | Output VAT | Credit (AED) | Deadline |",
            "|---|---|---|---|---|---|",
        ] + period_rows + [""]
        lines.append(f"✅ **Money you could claim back:** AED {total_credit:,.2f}")
        if at_risk:
            lines.append("")
            lines.append(f"⚠️ **Expiring within 180 days:** AED {at_risk:,.2f}")
    elif invoices:
        total_vat = round(sum(abs(i["vat_amount"]) for i in invoices), 2)
        lines.append(f"🧾 **{len(invoices)} invoice(s), total input VAT AED {total_vat:,.2f}.** "
                     "Attach the filed VAT returns too and I can work out the credit per period.")
    else:
        lines.append("I couldn't find invoices or VAT return figures in these files.")

    problems = []
    if bad_vat:
        problems.append(f"{len(bad_vat)} invoice(s) where VAT is not 5% of the taxable amount")
    if bad_trn:
        problems.append(f"{len(bad_trn)} invoice(s) without a valid 15-digit supplier TRN")
    if dups:
        problems.append(f"{len(dups)} possible duplicate invoice(s)")
    if ledger:
        cn = [l for l in ledger if l["credit_aed"] > 0 and "credit note" in l["description"].lower()]
        if cn and not any(_is_credit_note(i.get("description"), i["taxable_amount"]) for i in invoices):
            problems.append(f"{len(cn)} credit note(s) in the ledger that are not in the invoices")
    lines.append("")
    lines.append("❌ **Problems:** " + ("; ".join(problems) if problems else "none found"))
    lines.append("")
    lines.append("_Nothing was saved. To keep these files and include them in your official numbers, "
                 "upload them in **☁️ Upload documents**._")
    return {
        "markdown": "\n".join(lines),
        "totals": {"credit": round(total_credit, 2), "at_risk": round(at_risk, 2),
                   "invoices": len(invoices), "periods": len(returns)},
    }
