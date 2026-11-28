"""Decide what an uploaded file really is, from its content.

Types: "register", "invoice", "vat_return", "ledger", "fta_guide", or "other"
(not a VAT document: photos, lecture notes, unrelated spreadsheets...).

A file has to *prove* it is a VAT document:
- Tax invoice: a TRN or the words "tax invoice", VAT wording and real amounts.
- VAT return: tax period + output tax + input tax wording.
- Excel / CSV: the right columns (invoice no. + VAT amount, period + output/input tax,
  or debit + credit).
- FTA guidance: long legal text from the FTA (kept as reference only).
Text is read with pdfplumber for text PDFs and with double-pass OCR for scans and
photos. The file name is never enough on its own.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

TABLE_EXT = (".csv", ".xlsx", ".xls")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

DOC_TYPE_LABELS = {
    "register": "Invoice register (Excel / CSV)",
    "invoice": "Tax invoice",
    "vat_return": "Filed VAT return (Form 201)",
    "ledger": "General ledger",
    "fta_guide": "FTA guidance",
    "evidence": "Supporting evidence (customs / statement)",
    "other": "Not a VAT document",
}

_TRN = re.compile(r"(?<!\d)100\d{12}(?!\d)")
_AMOUNT = re.compile(r"(?<![\d.])\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})(?![\d])|(?<![\d.])\d{4,}\.\d{2}(?![\d])")
_DATE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4})\b", re.I)


_OUT_WORDS = ("output tax", "output vat", "vat on sales", "sales and all other outputs", "due tax",
              "total value of due tax", "vat due", "ضريبة المخرجات")
_IN_WORDS = ("input tax", "input vat", "recoverable input", "recoverable tax", "vat on expenses",
             "expenses and all other inputs", "vat on purchases", "total value of recoverable tax", "ضريبة المدخلات")
# Q1 2026 · 2026-Q1 · Q1-2026 · Jan-Mar 2026 · 01/2026
_PERIOD = re.compile(r"\b(?:q[1-4][\s/-]*20\d{2}|20\d{2}[\s/-]*q[1-4]|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*[-–]\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+20\d{2})\b", re.I)


def _period_rows(raw: str) -> int:
    """Lines that start with a tax period and carry at least two numbers (a return summary table)."""
    n = 0
    for line in raw.splitlines():
        m = _PERIOD.search(line)
        if m and len(re.findall(r"-?\(?\d[\d,]*(?:\.\d+)?\)?", line[m.end():])) >= 2:
            n += 1
    return n


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def _count(text: str, *words: str) -> int:
    return sum(text.count(w) for w in words)


def _trns(text: str) -> List[str]:
    # OCR sometimes splits long numbers with spaces: join digit groups first
    joined = re.sub(r"(?<=\d)[  ](?=\d)", "", text)
    return list(dict.fromkeys(_TRN.findall(joined)))


# ------------------------------------------------------------------ text extraction

def document_text(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Readable text of a PDF or image: pdfplumber first, double-pass OCR for scans/photos."""
    suffix = Path(filename).suffix.lower()
    text, method, conf = "", "none", None
    if suffix == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages[:8])
            method = "pdf text"
        except Exception:
            text = ""
        if len(text.strip()) < 40:
            try:
                import tempfile
                from ingest.ocr_engine import extract_ocr_from_scanned_pdf
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(file_bytes)
                    tmp_path = Path(tmp.name)
                res = extract_ocr_from_scanned_pdf(tmp_path)
                tmp_path.unlink(missing_ok=True)
                text, method, conf = _ocr_text(res), "ocr (2 passes)", res.get("avg_confidence")
            except Exception:
                method = "ocr unavailable"
    elif suffix in IMAGE_EXT:
        try:
            from ingest.ocr_engine import extract_ocr_from_image
            res = extract_ocr_from_image(file_bytes)
            text, method, conf = _ocr_text(res), "ocr (2 passes)", res.get("avg_confidence")
        except Exception:
            method = "ocr unavailable"
    return {"text": text or "", "method": method, "confidence": conf}


# ------------------------------------------------------------------ text documents

def _ocr_text(res: Dict[str, Any]) -> str:
    """Rows rebuilt from word positions first (keeps table rows together), then every line from both passes."""
    rows = res.get("row_text") or ""
    return (rows + "\n" + res.get("text", "")).strip() if rows else res.get("text", "")


def classify_text(text: str, filename: str = "") -> Dict[str, Any]:
    """Classify PDF / image text. Returns type, confidence, reasons, scores."""
    raw = text or ""
    t = raw.lower()
    words = len(t.split())
    trns = _trns(raw)
    amounts = _AMOUNT.findall(raw)
    dates = _DATE.findall(raw)
    vat_words = _has(t, "vat", "value added tax", "ضريبة القيمة المضافة", "5%", "5 %")

    inv, inv_why = 0, []
    if _has(t, "tax invoice", "فاتورة ضريبية", "simplified tax invoice"):
        inv += 4; inv_why.append('says "tax invoice"')
    if trns:
        inv += min(len(trns), 2) * 2; inv_why.append(f"{len(trns)} TRN(s)")
    if vat_words:
        inv += 2; inv_why.append("VAT wording")
    if re.search(r"invoice\s*(no|number|#|date)", t):
        inv += 2; inv_why.append("invoice number/date")
    if len(amounts) >= 2:
        inv += 2; inv_why.append(f"{len(amounts)} amounts")
    if _has(t, "total"):
        inv += 1
    if _has(t, "aed", "dhs", "dirham", "د.إ"):
        inv += 1
    if dates:
        inv += 1
    is_invoice = (inv >= 8 and len(amounts) >= 2 and (trns or "tax invoice" in t) and vat_words)

    ret, ret_why = 0, []
    if re.search(r"vat\s*-?\s*201|form\s*-?\s*201|vat\s+returns?\b|return\s+summar|اقرار\s*ضريبي|إقرار\s*ضريبي", t):
        ret += 4; ret_why.append("VAT return / Form 201 title")
    if _has(t, "tax period", "return period") or _PERIOD.search(raw):
        ret += 2; ret_why.append("tax period")
    has_out = _has(t, *_OUT_WORDS)
    has_in = _has(t, *_IN_WORDS)
    if has_out:
        ret += 2; ret_why.append("output VAT (VAT on sales)")
    if has_in:
        ret += 2; ret_why.append("input VAT (recoverable VAT on purchases)")
    if _has(t, "net vat", "net position", "payable tax", "net payable", "refundable", "net vat due"):
        ret += 1; ret_why.append("net VAT position")
    period_rows = _period_rows(raw)
    if period_rows:
        ret += min(period_rows, 3); ret_why.append(f"{period_rows} tax period row(s) with figures")
    if trns:
        ret += 1
    if amounts or period_rows:
        ret += 1
    # Words alone are not enough (lecture notes talk about output and input tax):
    # a return must carry figures, i.e. period rows with numbers or real money amounts.
    has_figures = period_rows >= 1 or len(amounts) >= 2
    is_return = ret >= 7 and has_out and has_in and has_figures

    guide, guide_why = 0, []
    if _has(t, "federal tax authority", "الهيئة الاتحادية للضرائب"):
        guide += 2; guide_why.append("Federal Tax Authority")
    if _has(t, "public clarification", "vat guide", "user guide", "executive regulation"):
        guide += 2; guide_why.append("guidance wording")
    legal = _count(t, "decree-law", "article ", "cabinet decision")
    guide += min(legal, 4)
    if words > 600:
        guide += 2
    is_guide = guide >= 6 and not is_invoice and not is_return

    # Supporting evidence: customs import declarations, VAT credit balance / account statements.
    ev, ev_why = 0, []
    if _has(t, "customs declaration", "import declaration", "bill of entry", "customs entry", "declaration no",
            "import vat", "vat on imports", "customs", "البيان الجمركي"):
        ev += 4; ev_why.append("customs / import VAT wording")
    if _has(t, "credit balance", "statement of account", "account statement", "balance statement", "opening balance",
            "closing balance", "carried forward", "brought forward"):
        ev += 4; ev_why.append("balance statement wording")
    if vat_words:
        ev += 1
    if amounts or period_rows:
        ev += 1
    is_evidence = ev >= 5 and not _has(t, "tax invoice", "فاتورة ضريبية")

    scores = {"invoice": inv, "vat_return": ret, "fta_guide": guide, "evidence": ev}
    if is_evidence and not (is_return and ret >= 12):
        return _result("evidence", ev, 8, ev_why, scores)
    if is_return and (ret >= inv or not is_invoice):
        return _result("vat_return", ret, 12, ret_why, scores)
    if is_invoice:
        return _result("invoice", inv, 16, inv_why, scores)
    if is_guide:
        return _result("fta_guide", guide, 12, guide_why, scores)

    if len(t.strip()) < 20:
        why = ["no readable text (looks like a photo or a blank page)"]
    else:
        missing = []
        if not trns and "tax invoice" not in t:
            missing.append("no TRN or 'tax invoice' heading")
        if not vat_words:
            missing.append("no VAT wording")
        if len(amounts) < 2:
            missing.append("no invoice amounts")
        why = missing or ["does not match a tax invoice, VAT return or ledger"]
    best = max(scores, key=scores.get)
    return {"type": "other", "confidence": 0.9, "reasons": why, "scores": scores, "best_guess": best}


def _result(kind: str, score: int, full: int, why: List[str], scores: Dict[str, int]) -> Dict[str, Any]:
    return {"type": kind, "confidence": round(min(1.0, score / full), 2), "reasons": why,
            "scores": scores, "best_guess": kind}


# ------------------------------------------------------------------ tables

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9؀-ۿ ]+", " ", str(s).lower()).strip()


def _classify_columns(columns: List[Any], n_rows: int) -> Dict[str, Any]:
    cols = [_norm(c) for c in columns]

    def col(*keys: str) -> bool:
        # short keys (dr, cr, no) must be whole words: 'address' is not a debit column
        for c in cols:
            for k in keys:
                if (len(k) <= 3 and re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", c)) or (len(k) > 3 and k in c):
                    return True
        return False

    reg = ret = led = 0
    reg_why, ret_why, led_why = [], [], []
    inv_no = col("invoice no", "invoice number", "invoice id", "inv no", "bill no", "invoice #", "invoice ref",
                 "document no", "رقم الفاتورة")
    if inv_no:
        reg += 3; reg_why.append("invoice number column")
    if col("vat", "tax amount", "ضريبة"):
        reg += 2; reg_why.append("VAT column")
    if col("supplier", "vendor", "seller", "customer", "client", "المورد", "العميل"):
        reg += 2; reg_why.append("supplier/customer column")
    if col("trn"):
        reg += 1
    if col("taxable", "net amount", "amount excl", "subtotal", "net value"):
        reg += 1
    if col("date"):
        reg += 1
    has_out = col("output tax", "output vat", "vat on sales", "box 8", "due tax", "ضريبة المخرجات")
    has_in = col("input tax", "input vat", "vat on purchases", "vat on expenses", "box 11", "recoverable", "ضريبة المدخلات")
    if col("tax period", "period"):
        ret += 3; ret_why.append("tax period column")
    if has_out:
        ret += 3; ret_why.append("output VAT column")
    if has_in:
        ret += 3; ret_why.append("input VAT column")
    has_dr, has_cr = col("debit", "dr", "مدين"), col("credit", "cr", "دائن")
    if has_dr:
        led += 3; led_why.append("debit column")
    if has_cr:
        led += 3; led_why.append("credit column")
    if col("account"):
        led += 2; led_why.append("account column")

    scores = {"register": reg, "vat_return": ret, "ledger": led}
    if ret >= 6 and has_out and has_in and not inv_no:
        return _result("vat_return", ret, 9, ret_why, scores)
    if led >= 6 and has_dr and has_cr:
        return _result("ledger", led, 8, led_why, scores)
    if reg >= 5 and inv_no and col("vat", "tax amount", "ضريبة", "taxable"):
        return _result("register", reg, 9, reg_why, scores)
    why = ["the table is empty"] if n_rows == 0 else [
        f"columns ({', '.join(str(c) for c in list(columns)[:6])}) don't match an invoice register, VAT return or ledger"]
    return {"type": "other", "confidence": 0.85, "reasons": why, "scores": scores,
            "best_guess": max(scores, key=scores.get)}


def classify_table(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Classify a CSV / workbook. Every sheet is checked and the header row is found automatically."""
    from ingest.table_loader import load_tables
    try:
        sheets = load_tables(file_bytes, filename, max_rows=80)
    except Exception as exc:
        sheets = []
        err = str(exc)
    else:
        err = ""
    if not sheets:
        return {"type": "other", "confidence": 0.9, "reasons": [f"could not read the table {err}".strip()],
                "scores": {}, "best_guess": "register"}
    results = []
    digest = "\n".join(f"[{name}]\n" + df.head(40).to_string(index=False, max_colwidth=40) for name, df in sheets)[:20000]
    for name, df in sheets:
        r = _classify_columns(list(df.columns), len(df))
        if r["type"] != "other" and len(sheets) > 1:
            r["reasons"] = [f"sheet '{name}'"] + r["reasons"]
        results.append(r)
    for r in results:
        r["text"] = digest
    found = [r for r in results if r["type"] != "other"]
    if not found:
        return results[0]
    # a workbook with purchase and sales sheets is a register; otherwise the strongest sheet wins
    best = max(found, key=lambda r: (r["type"] == "register", r["confidence"]))
    if len({r["type"] for r in found}) > 1:
        best = max(found, key=lambda r: r["confidence"])
    return best


# ------------------------------------------------------------------ public API

def classify_document(file_bytes: bytes, filename: str, text: Optional[str] = None) -> Dict[str, Any]:
    """Full classification: type, confidence, reasons, method, text (for PDFs/images)."""
    suffix = Path(filename).suffix.lower()
    if suffix in TABLE_EXT:
        res = classify_table(file_bytes, filename)
        res.update({"method": "table columns", "text": res.get("text")})
        return res
    if text is None:
        info = document_text(file_bytes, filename)
        text, method = info["text"], info["method"]
    else:
        method = "stored text"
    if suffix not in (".pdf",) + IMAGE_EXT:
        return {"type": "other", "confidence": 0.9, "reasons": [f"{suffix or 'this'} files are not supported"],
                "scores": {}, "best_guess": "other", "method": "none", "text": text}
    if method == "ocr unavailable":
        return {"type": "other", "confidence": 0.3, "reasons": ["text could not be read on this server (OCR unavailable)"],
                "scores": {}, "best_guess": "invoice", "method": method, "text": ""}
    res = classify_text(text, filename)
    res.update({"method": method, "text": text})
    return res


def detect_document_type(file_bytes: bytes, filename: str) -> str:
    """Backward-compatible helper: just the type."""
    return classify_document(file_bytes, filename)["type"]
