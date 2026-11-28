"""Read stored files into the database, and repair files saved under the wrong type.

`extract_into_db` is the non-interactive reader shared by uploads and repairs.
`repair_document_types` re-checks every stored file from its content (the
original bytes are read back from Cloudflare R2 / local storage) and, when the
stored type is wrong or nothing could be read, removes the wrong rows and reads
the file again with the right reader. No re-upload is needed.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.connection import get_db_connection
from ingest.doc_classifier import classify_document, DOC_TYPE_LABELS

# Bump when the readers improve: stored files read by an older version are read again once.
# 2 = invoice reader with real dates / sales vs purchases, multi-sheet registers, evidence type.
READER_VERSION = 2


def _insert_invoice(business_id: int, doc_id: int, inv: Dict[str, Any], status: str = "VALID",
                    reason: Optional[str] = None, db_path: Optional[Path] = None) -> None:
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO invoices (
                    business_id, document_id, direction, supplier_name, supplier_trn, customer_name, customer_trn,
                    invoice_number, date, taxable_amount, vat_amount, total, currency,
                    description_lines, eligibility_status, eligibility_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    business_id, doc_id, inv.get("direction", "purchase"), inv.get("supplier_name") or "Supplier",
                    inv.get("supplier_trn"), inv.get("customer_name"), inv.get("customer_trn"), inv.get("invoice_number") or f"INV-{doc_id}",
                    str(inv.get("date") or date.today().isoformat())[:10],
                    float(inv.get("taxable_amount", 0.0) or 0.0), float(inv.get("vat_amount", 0.0) or 0.0),
                    float(inv.get("total", 0.0) or 0.0), inv.get("currency") or "AED",
                    json.dumps(inv.get("description_lines", []) or []), status, reason,
                ),
            )
    finally:
        conn.close()


def extract_into_db(business_id: int, doc_id: int, ftype: str, file_bytes: bytes, filename: str,
                    db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read one file with the reader for `ftype` and store the rows. Returns {'records': n, 'note': str}."""
    name = filename.lower()
    if ftype == "register":
        from ingest.excel_parser import parse_excel_register
        recs, _ = parse_excel_register(file_bytes=file_bytes, filename=filename, custom_mapping=None, business_id=business_id)
        for r in recs:
            if r.get("date"):
                _insert_invoice(business_id, doc_id, r, db_path=db_path)
            else:
                _insert_invoice(business_id, doc_id, r, status="NEEDS_REVIEW",
                                reason="the invoice date could not be read", db_path=db_path)
        return {"records": len(recs), "note": f"{len(recs)} invoice line(s)"}

    if ftype == "evidence":
        imp = parse_customs_import_vat(file_bytes, filename)
        if imp:
            _insert_invoice(business_id, doc_id, imp, db_path=db_path)
            return {"records": 1, "note": f"import VAT AED {imp['vat_amount']:,.2f} from customs declaration"}
        return {"records": 0, "note": "kept as supporting evidence"}

    if ftype == "vat_return":
        from ingest.returns_ledger_parser import parse_vat_returns, save_vat_returns
        n = save_vat_returns(business_id, doc_id, parse_vat_returns(file_bytes, filename), db_path=db_path)
        return {"records": n, "note": f"{n} VAT return period(s)"}

    if ftype == "ledger":
        from ingest.returns_ledger_parser import parse_general_ledger, save_ledger_entries
        n = save_ledger_entries(business_id, doc_id, parse_general_ledger(file_bytes, filename), db_path=db_path)
        return {"records": n, "note": f"{n} ledger line(s)"}

    if ftype == "invoice":
        from ingest.extractor import extract_invoice_fields
        text, uncertain = "", False
        if name.endswith(".pdf"):
            from ingest.pdf_parser import extract_text_from_pdf
            text = extract_text_from_pdf(file_bytes).get("text", "").strip()
        if len(text) < 30:
            try:
                from ingest.ocr_engine import extract_ocr_from_image, extract_ocr_from_scanned_pdf
                if name.endswith(".pdf"):
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp.write(file_bytes)
                        tmp_path = Path(tmp.name)
                    res = extract_ocr_from_scanned_pdf(tmp_path)
                    tmp_path.unlink(missing_ok=True)
                else:
                    res = extract_ocr_from_image(file_bytes)
                text = ((res.get("row_text") or "") + "\n" + res.get("text", "")).strip()
                uncertain = res.get("avg_confidence", 0.0) < 0.70
            except Exception:
                return {"records": 0, "note": "scanned file could not be read on this server"}
        inv = extract_invoice_fields(text, source_document=filename, business_id=business_id, db_path=db_path)
        status = "NEEDS_REVIEW" if (uncertain or inv.eligibility_status == "NEEDS_REVIEW") else inv.eligibility_status
        reason = "Uncertain scan - please confirm the amounts" if uncertain else inv.eligibility_reason
        _insert_invoice(business_id, doc_id, {
            "direction": inv.direction, "supplier_name": inv.supplier_name, "supplier_trn": inv.supplier_trn,
            "customer_name": inv.customer_name, "customer_trn": inv.customer_trn,
            "invoice_number": inv.invoice_number, "date": inv.date.isoformat(), "taxable_amount": inv.taxable_amount,
            "vat_amount": inv.vat_amount, "total": inv.total, "currency": inv.currency,
            "description_lines": inv.description_lines,
        }, status=status, reason=reason, db_path=db_path)
        return {"records": 1, "note": f"invoice {inv.invoice_number}, VAT AED {inv.vat_amount:,.2f}"}

    return {"records": 0, "note": "kept as reference (no numbers read)"}


def _reader_versions(business_id: int, db_path: Optional[Path] = None) -> Dict[int, int]:
    conn = get_db_connection(db_path=db_path, readonly=True)
    try:
        rows = conn.execute("SELECT id, COALESCE(reader_version, 0) AS v FROM uploaded_documents WHERE business_id = ?;",
                            (business_id,)).fetchall()
        return {int(r["id"]): int(r["v"]) for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def _set_version(doc_id: int, db_path: Optional[Path] = None) -> None:
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            conn.execute("UPDATE uploaded_documents SET reader_version = ? WHERE id = ?;", (READER_VERSION, doc_id))
    except Exception:
        pass
    finally:
        conn.close()


def _stored_texts(business_id: int, db_path: Optional[Path] = None) -> Dict[int, str]:
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, extracted_text FROM uploaded_documents WHERE business_id = ?;", (business_id,))
        return {int(r["id"]): r["extracted_text"] for r in cur.fetchall() if r["extracted_text"]}
    finally:
        conn.close()


def _save_text(business_id: int, doc_id: int, text: str, db_path: Optional[Path] = None) -> None:
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            conn.execute("UPDATE uploaded_documents SET extracted_text = ? WHERE id = ? AND business_id = ?;",
                         (text[:20000], doc_id, business_id))
    finally:
        conn.close()


def repair_document_types(business_id: int, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Re-check every stored file and re-read the ones saved under the wrong type (or with nothing read)."""
    from ingest.document_manager import list_cloud_documents
    from ingest.returns_ledger_parser import prepare_document_for_reprocessing
    from ingest.storage_interface import get_storage_backend

    backend = get_storage_backend()
    fixes: List[Dict[str, Any]] = []
    stored_text = _stored_texts(business_id, db_path=db_path)
    versions = _reader_versions(business_id, db_path=db_path)
    for d in list_cloud_documents(business_id, db_path=db_path):
        outdated = versions.get(d["id"], 0) < READER_VERSION
        if d["document_type"] in ("fta_guide", "other") or (d["document_type"] == "evidence" and not outdated):
            continue
        try:
            data = backend.read_file(d["storage_path"])
        except Exception:
            continue
        txt = stored_text.get(d["id"])
        cls = classify_document(data, d["original_filename"], text=txt or None)
        if not txt and cls.get("text"):
            _save_text(business_id, d["id"], cls["text"], db_path=db_path)
        detected = cls["type"]
        if detected == d["document_type"] and not outdated:
            continue  # already read by the current reader (0 rows can be legitimate, e.g. a second copy)
        if detected == d["document_type"] and outdated and detected in ("fta_guide", "other"):
            _set_version(d["id"], db_path=db_path)
            continue
        if detected in ("fta_guide", "evidence"):
            # Wrongly saved as an invoice/return earlier: remove the fake rows and relabel it.
            if detected == "evidence" and (d["document_type"] != "evidence" or outdated):
                prepare_document_for_reprocessing(d["id"], business_id, "evidence", db_path=db_path, force=True)
                extract_into_db(business_id, d["id"], "evidence", data, d["original_filename"], db_path=db_path)
                _set_version(d["id"], db_path=db_path)
                fixes.append({
                    "file_name": d["original_filename"],
                    "old_type": DOC_TYPE_LABELS.get(d["document_type"], d["document_type"]),
                    "new_type": DOC_TYPE_LABELS["evidence"],
                    "result": "kept as supporting evidence; no invoice rows read from it",
                })
            continue
        if detected == "other":
            # Not a VAT document (photo, notes...): remove any numbers wrongly read from it.
            if not d["included_in_analysis"]:
                from ingest.document_manager import set_analysis_selection
                current = [x["id"] for x in list_cloud_documents(business_id, db_path=db_path) if x["included_in_analysis"]]
                set_analysis_selection(business_id, current + [d["id"]], db_path=db_path)
            prepare_document_for_reprocessing(d["id"], business_id, "other", db_path=db_path)
            _set_version(d["id"], db_path=db_path)
            fixes.append({
                "file_name": d["original_filename"],
                "old_type": DOC_TYPE_LABELS.get(d["document_type"], d["document_type"]),
                "new_type": DOC_TYPE_LABELS["other"],
                "result": "not used in your numbers any more (" + "; ".join(cls["reasons"][:2]) + ")",
            })
            continue
        was_included = d["included_in_analysis"]
        if not was_included:
            # bring parked rows back first so they are cleaned up too
            from ingest.document_manager import set_analysis_selection
            current = [x["id"] for x in list_cloud_documents(business_id, db_path=db_path) if x["included_in_analysis"]]
            set_analysis_selection(business_id, current + [d["id"]], db_path=db_path)
        prepare_document_for_reprocessing(d["id"], business_id, detected, db_path=db_path, force=True)
        res = extract_into_db(business_id, d["id"], detected, data, d["original_filename"], db_path=db_path)
        _set_version(d["id"], db_path=db_path)
        if not was_included:
            from ingest.document_manager import set_analysis_selection
            current = [x["id"] for x in list_cloud_documents(business_id, db_path=db_path)
                       if x["included_in_analysis"] and x["id"] != d["id"]]
            set_analysis_selection(business_id, current, db_path=db_path)
        fixes.append({
            "file_name": d["original_filename"],
            "old_type": DOC_TYPE_LABELS.get(d["document_type"], d["document_type"]),
            "new_type": DOC_TYPE_LABELS.get(detected, detected),
            "result": res["note"],
        })
    return fixes



def parse_customs_import_vat(file_bytes: bytes, filename: str) -> Optional[Dict[str, Any]]:
    """Import VAT paid at customs is recoverable input tax; the customs declaration is its proof.

    Reads the declaration number, date, customs value and import VAT. Returns an invoice-like
    purchase row (supplier 'UAE Customs (import VAT)') or None if this is not a customs document.
    """
    import re
    from ingest.doc_classifier import document_text
    text = document_text(file_bytes, filename).get("text", "") or ""
    low = text.lower()
    if not any(w in low for w in ("customs", "import declaration", "bill of entry", "import vat")):
        return None

    def money_after(*labels: str) -> Optional[float]:
        for lab in labels:
            m = re.search(lab + r"[^\n\d]{0,40}?([\d,]+\.\d{2}|[\d,]{3,})", text, re.I)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
        return None

    vat = money_after(r"import vat", r"vat on imports?", r"vat\s*\(5\s*%\)", r"vat amount", r"\bvat\b")
    if not vat or vat <= 0:
        return None
    value = money_after(r"customs value", r"cif value", r"value for vat", r"taxable value") or round(vat / 0.05, 2)
    decl = re.search(r"(?:declaration|bill of entry|entry)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{4,})", text, re.I)
    from ingest.doc_classifier import _DATE
    d_iso = None
    for raw in _DATE.findall(text):
        from ingest.returns_ledger_parser import _to_iso
        d_iso = _to_iso(raw)
        if d_iso:
            break
    if not d_iso:
        return None
    return {
        "direction": "purchase",
        "supplier_name": "UAE Customs (import VAT)",
        "supplier_trn": None,
        "invoice_number": (decl.group(1) if decl else f"CUSTOMS-{abs(hash(text)) % 10**6}")[:40],
        "date": d_iso,
        "taxable_amount": value,
        "vat_amount": vat,
        "total": round(value + vat, 2),
        "description_lines": ["Import VAT paid at customs (recoverable input tax)"],
    }
