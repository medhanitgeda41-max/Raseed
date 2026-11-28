"""Read filed VAT returns (Form 201) and General Ledger VAT exports into the database.

Supported inputs
- VAT returns: CSV / XLSX with one row per tax period, or a text PDF of a Form 201
  summary (period, period start/end, output tax, input tax).
- General ledger: CSV / XLSX with date, account code/name, description, debit, credit.

All arithmetic stays deterministic; nothing here calls an AI model.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from database.connection import get_db_connection

RETURN_ALIASES: Dict[str, List[str]] = {
    "period": ["tax period", "period", "return period", "period code", "الفترة الضريبية"],
    "period_start": ["period start", "start date", "from", "period from", "بداية الفترة"],
    "period_end": ["period end", "end date", "to", "period to", "نهاية الفترة"],
    "output_tax": ["output tax", "output vat", "vat on sales", "total output tax", "box 8", "due tax",
                   "total value of due tax", "ضريبة المخرجات"],
    "input_tax": ["input tax", "input vat", "recoverable input tax", "vat on purchases", "vat on expenses", "box 11",
                  "recoverable tax", "total value of recoverable tax", "ضريبة المدخلات"],
    "status": ["status", "return status", "filing status", "الحالة"],
    "filing_date": ["filing date", "filed on", "submission date", "date filed", "تاريخ التقديم"],
}

LEDGER_ALIASES: Dict[str, List[str]] = {
    "entry_date": ["entry date", "posting date", "date", "transaction date", "التاريخ"],
    "account_code": ["account code", "account no", "account number", "gl code", "code", "رمز الحساب"],
    "account_name": ["account name", "account", "gl account", "اسم الحساب"],
    "description": ["description", "narration", "memo", "details", "البيان"],
    "debit_aed": ["debit", "debit aed", "dr", "مدين"],
    "credit_aed": ["credit", "credit aed", "cr", "دائن"],
    "vat_code": ["vat code", "tax code", "رمز الضريبة"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9؀-ۿ ]+", " ", str(s).lower()).strip()


def _map_columns(columns: List[str], aliases: Dict[str, List[str]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    normed = {c: _norm(c) for c in columns}
    for field, names in aliases.items():
        # exact alias first, then "contains"
        for c, n in normed.items():
            if c not in mapping.values() and n in [_norm(a) for a in names]:
                mapping[field] = c
                break
        if field in mapping:
            continue
        for c, n in normed.items():
            if c not in mapping.values() and any(_norm(a) in n for a in names if len(_norm(a)) > 3):
                mapping[field] = c
                break
    return mapping


def _read_table(file_bytes: bytes, filename: str) -> pd.DataFrame:
    if Path(filename).suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(file_bytes))
    try:
        return pd.read_csv(io.BytesIO(file_bytes))
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(file_bytes), encoding="latin1")


def _to_float(v: Any) -> float:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    s = str(v).replace(",", "").replace("AED", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_iso(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return None
    if isinstance(v, (datetime, date)):
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s, dayfirst=True).date().isoformat()
    except Exception:
        return None


def _quarter_code(start_iso: str) -> str:
    d = date.fromisoformat(start_iso)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _period_dates(text: Any) -> Optional[tuple]:
    """'Q1 2026' / '2026-Q1' / 'Q1-2026' / 'Jan-Mar 2026' / 'January 2026' -> (code, start, end)."""
    import calendar
    s = str(text or "").strip().lower()
    m = re.search(r"q([1-4])[\s/-]*(20\d{2})", s) or re.search(r"(20\d{2})[\s/-]*q([1-4])", s)
    if m:
        a, b = m.groups()
        q, y = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        sm, em = 3 * q - 2, 3 * q
        return f"{y}-Q{q}", date(y, sm, 1).isoformat(), date(y, em, calendar.monthrange(y, em)[1]).isoformat()
    m = re.search(r"([a-z]{3})[a-z]*\s*[-–]\s*([a-z]{3})[a-z]*\s+(20\d{2})", s)
    if m and m.group(1) in _MONTHS and m.group(2) in _MONTHS:
        y, sm, em = int(m.group(3)), _MONTHS[m.group(1)], _MONTHS[m.group(2)]
        st = date(y, sm, 1)
        code = f"{y}-Q{(sm - 1) // 3 + 1}" if em - sm == 2 and sm % 3 == 1 else f"{y}-{sm:02d}"
        return code, st.isoformat(), date(y, em, calendar.monthrange(y, em)[1]).isoformat()
    m = re.search(r"\b([a-z]{3})[a-z]*\s+(20\d{2})\b", s)
    if m and m.group(1) in _MONTHS:
        y, mo = int(m.group(2)), _MONTHS[m.group(1)]
        return f"{y}-{mo:02d}", date(y, mo, 1).isoformat(), date(y, mo, calendar.monthrange(y, mo)[1]).isoformat()
    return None


def _clean_return(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "draft" in str(row.get("status") or "").lower():
        return None  # a draft is not a filed return
    start = _to_iso(row.get("period_start"))
    end = _to_iso(row.get("period_end"))
    pd_info = _period_dates(row.get("period"))
    if (not start or not end) and pd_info:
        start, end = pd_info[1], pd_info[2]
    if not start or not end:
        return None
    period = (pd_info[0] if pd_info else str(row.get("period") or "").strip()) or _quarter_code(start)
    out_t = abs(_to_float(row.get("output_tax")))
    in_t = abs(_to_float(row.get("input_tax")))
    return {
        "period": period,
        "period_start": start,
        "period_end": end,
        "output_tax": round(out_t, 2),
        "input_tax": round(in_t, 2),
        "net_payable_refundable": round(out_t - in_t, 2),
        "filing_date": _to_iso(row.get("filing_date")),
    }


def _returns_from_pdf_text(text: str) -> List[Dict[str, Any]]:
    """Read one Form 201 summary from PDF text (label: value lines)."""
    def grab(labels: List[str]) -> Optional[str]:
        for lab in labels:
            m = re.search(rf"{lab}\s*[:\-]?\s*(?:AED\s*)?([^\n]+)", text, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    def money(labels: List[str]) -> Optional[str]:
        v = grab(labels)
        if not v:
            return None
        m = re.search(r"\(?-?[\d,]+(?:\.\d+)?\)?", v)
        return m.group(0) if m else None

    row = {
        "period": grab([r"Tax Period", r"Return Period", r"VAT Return Period"]),
        "period_start": grab([r"Period Start", r"Period From", r"Start Date"]),
        "period_end": grab([r"Period End", r"Period To", r"End Date"]),
        "output_tax": money([r"Total value of due tax for the period", r"Total value of due tax", r"Total Output Tax",
                             r"Output Tax", r"Output VAT", r"Box 8[^:\n]*"]),
        "input_tax": money([r"Total value of recoverable tax for the period", r"Total value of recoverable tax",
                            r"Recoverable Input Tax", r"Total Input Tax", r"Input Tax", r"Input VAT", r"Box 11[^:\n]*",
                            r"Box 10[^:\n]*"]),
        "filing_date": grab([r"Filing Date", r"Date Filed", r"Submitted On", r"Date of Submission"]),
    }
    per = row["period"] or ""
    two = re.findall(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}", per)
    if len(two) >= 2:
        row["period_start"] = row["period_start"] or two[0]
        row["period_end"] = row["period_end"] or two[1]
        row["period"] = None
    elif row["period"]:
        row["period"] = row["period"].split()[0] if not re.search(r"q[1-4]|[a-z]{3}", per, re.I) else per
    cleaned = _clean_return(row)
    return [cleaned] if cleaned else []


def _returns_from_rows(header: List[Any], body: List[List[Any]]) -> List[Dict[str, Any]]:
    mapping = _map_columns([str(c or "") for c in header], RETURN_ALIASES)
    if "output_tax" not in mapping or "input_tax" not in mapping:
        return []
    idx = {f: [str(c or "") for c in header].index(c) for f, c in mapping.items()}
    out: List[Dict[str, Any]] = []
    for r in body:
        raw = {f: (r[i] if i < len(r) else None) for f, i in idx.items()}
        cleaned = _clean_return(raw)
        if cleaned:
            out.append(cleaned)
    return out


def _returns_from_pdf_tables(file_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        import pdfplumber
        rows: List[Dict[str, Any]] = []
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages[:10]:
                for tbl in page.extract_tables() or []:
                    if tbl and len(tbl) > 1:
                        rows += _returns_from_rows(tbl[0], tbl[1:])
        return rows
    except Exception:
        return []


def _returns_from_text_lines(text: str) -> List[Dict[str, Any]]:
    """Summary tables without ruling: 'Q1 2026  42000  51000  -9000  Filed'.
    Column order is taken from the header line (output before input, or the reverse)."""
    low = text.lower()
    o = min([low.find(w) for w in ("output", "due tax", "vat on sales") if w in low] or [-1])
    i = min([low.find(w) for w in ("input", "recoverable", "vat on expenses", "vat on purchases") if w in low] or [-1])
    if o < 0 or i < 0:
        return []
    out_first = o < i
    per = re.compile(r"(q[1-4][\s/-]*20\d{2}|20\d{2}[\s/-]*q[1-4]|[a-z]{3}[a-z]*\s*[-–]\s*[a-z]{3}[a-z]*\s+20\d{2})", re.I)
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        m = per.search(line)
        if not m:
            continue
        nums = re.findall(r"\(?-?[\d,]+(?:\.\d+)?\)?", line[m.end():])
        nums = [n for n in nums if re.search(r"\d{2,}", n)]
        if len(nums) < 2:
            continue
        a, b = nums[0], nums[1]
        cleaned = _clean_return({"period": m.group(1), "output_tax": a if out_first else b,
                                 "input_tax": b if out_first else a, "status": line})
        if cleaned:
            rows.append(cleaned)
    return rows


def parse_vat_returns(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Parse filed VAT returns from CSV / XLSX / PDF (single Form 201, or a summary table of periods)."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
        if suffix == ".pdf":
            rows = _returns_from_pdf_tables(file_bytes)
            if rows:
                return rows
        # Text PDF, scanned PDF or photo: get the text (OCR with two passes when needed)
        from ingest.doc_classifier import document_text
        text = document_text(file_bytes, filename).get("text", "")
        return _returns_from_pdf_text(text) or _returns_from_text_lines(text)

    df = _read_table(file_bytes, filename)
    mapping = _map_columns([str(c) for c in df.columns], RETURN_ALIASES)
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        raw = {field: r[col] for field, col in mapping.items()}
        cleaned = _clean_return(raw)
        if cleaned:
            rows.append(cleaned)
    return rows


def parse_general_ledger(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Parse General Ledger VAT lines from CSV / XLSX."""
    df = _read_table(file_bytes, filename)
    mapping = _map_columns([str(c) for c in df.columns], LEDGER_ALIASES)
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        get = lambda f: r[mapping[f]] if f in mapping else None  # noqa: E731
        d = _to_iso(get("entry_date"))
        if not d:
            continue
        debit, credit = _to_float(get("debit_aed")), _to_float(get("credit_aed"))
        if debit < 0:
            credit, debit = credit + abs(debit), 0.0
        if credit < 0:
            debit, credit = debit + abs(credit), 0.0
        code = get("account_code")
        rows.append({
            "entry_date": d,
            "account_code": str(code).strip() if code is not None and not pd.isna(code) else "VAT",
            "account_name": str(get("account_name") or "VAT Account").strip(),
            "description": str(get("description") or "").strip() or "Ledger entry",
            "debit_aed": round(debit, 2),
            "credit_aed": round(credit, 2),
            "vat_code": (str(get("vat_code")).strip() if get("vat_code") is not None and not pd.isna(get("vat_code")) else None),
        })
    return rows


def save_vat_returns(business_id: int, document_id: Optional[int], rows: List[Dict[str, Any]],
                     db_path: Optional[Path] = None) -> int:
    """Insert or update filed returns (one per period). Returns number saved."""
    if not rows:
        return 0
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO vat_returns (business_id, document_id, period, period_start, period_end,
                        output_tax, input_tax, net_payable_refundable, filing_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(business_id, period) DO UPDATE SET
                        document_id = excluded.document_id,
                        period_start = excluded.period_start,
                        period_end = excluded.period_end,
                        output_tax = excluded.output_tax,
                        input_tax = excluded.input_tax,
                        net_payable_refundable = excluded.net_payable_refundable,
                        filing_date = excluded.filing_date;
                    """,
                    (business_id, document_id, r["period"], r["period_start"], r["period_end"],
                     r["output_tax"], r["input_tax"], r["net_payable_refundable"], r["filing_date"]),
                )
        return len(rows)
    finally:
        conn.close()


def save_ledger_entries(business_id: int, document_id: Optional[int], rows: List[Dict[str, Any]],
                        db_path: Optional[Path] = None) -> int:
    if not rows:
        return 0
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            for r in rows:
                conn.execute(
                    """
                    INSERT INTO ledger_entries (business_id, document_id, entry_date, account_code, account_name,
                        description, debit_aed, credit_aed, vat_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (business_id, document_id, r["entry_date"], r["account_code"], r["account_name"],
                     r["description"], r["debit_aed"], r["credit_aed"], r["vat_code"]),
                )
        return len(rows)
    finally:
        conn.close()


def link_ledger_to_invoices(business_id: int, db_path: Optional[Path] = None) -> int:
    """Link unmatched GL VAT debits to purchase invoices.

    A debit matches an invoice when the invoice number appears in the GL
    description, or when the VAT amount is the same (within 1 fils) and the
    dates are no more than 31 days apart. Each invoice is used once.
    Returns the number of new links.
    """
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, entry_date, description, debit_aed FROM ledger_entries "
            "WHERE business_id = ? AND debit_aed > 0 AND reconciled_invoice_id IS NULL;",
            (business_id,),
        )
        gl_rows = cur.fetchall()
        if not gl_rows:
            return 0
        cur.execute(
            "SELECT id, invoice_number, date, vat_amount FROM invoices "
            "WHERE business_id = ? AND direction = 'purchase';",
            (business_id,),
        )
        invoices = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT reconciled_invoice_id FROM ledger_entries "
            "WHERE business_id = ? AND reconciled_invoice_id IS NOT NULL;",
            (business_id,),
        )
        used = {r["reconciled_invoice_id"] for r in cur.fetchall()}

        def _d(v: Any) -> Optional[date]:
            try:
                return date.fromisoformat(str(v)[:10])
            except Exception:
                return None

        links = []
        for g in gl_rows:
            desc = str(g["description"] or "").upper()
            match = None
            for inv in invoices:
                num = str(inv["invoice_number"] or "").upper()
                if inv["id"] not in used and len(num) >= 4 and num in desc:
                    match = inv
                    break
            if match is None:
                gd = _d(g["entry_date"])
                for inv in invoices:
                    idt = _d(inv["date"])
                    if (inv["id"] not in used and abs(float(inv["vat_amount"]) - float(g["debit_aed"])) < 0.01
                            and gd and idt and abs((gd - idt).days) <= 31):
                        match = inv
                        break
            if match is not None:
                used.add(match["id"])
                links.append((match["id"], g["id"]))
        if links:
            with conn:
                for inv_id, gl_id in links:
                    conn.execute("UPDATE ledger_entries SET reconciled_invoice_id = ? WHERE id = ?;", (inv_id, gl_id))
        return len(links)
    finally:
        conn.close()


def prepare_document_for_reprocessing(document_id: int, business_id: int, new_type: str,
                                      db_path: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """Decide whether an already-uploaded file should be read again.

    Returns {"reprocess": bool, "old_type": str}. A file is read again when it was
    stored under a different type (e.g. a VAT return saved as a register), or when
    nothing was ever extracted from it. Rows previously extracted from the file are
    removed first so nothing is counted twice.
    """
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        cur.execute("SELECT document_type FROM uploaded_documents WHERE id = ? AND business_id = ?;",
                    (document_id, business_id))
        row = cur.fetchone()
        old_type = row["document_type"] if row else None
        counts = {}
        for tbl in ("invoices", "ledger_entries", "vat_returns"):
            cur.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE document_id = ? AND business_id = ?;",
                        (document_id, business_id))
            counts[tbl] = int(cur.fetchone()["c"])
        extracted = sum(counts.values())
        reprocess = force or (old_type != new_type) or extracted == 0
        if reprocess:
            with conn:
                for tbl in ("invoices", "ledger_entries", "vat_returns"):
                    if tbl == "invoices":
                        # ledger lines may point at these invoices
                        conn.execute(
                            "UPDATE ledger_entries SET reconciled_invoice_id = NULL WHERE business_id = ? AND "
                            "reconciled_invoice_id IN (SELECT id FROM invoices WHERE document_id = ? AND business_id = ?);",
                            (business_id, document_id, business_id),
                        )
                    conn.execute(f"DELETE FROM {tbl} WHERE document_id = ? AND business_id = ?;",
                                 (document_id, business_id))
                conn.execute("UPDATE uploaded_documents SET document_type = ? WHERE id = ? AND business_id = ?;",
                             (new_type, document_id, business_id))
        return {"reprocess": reprocess, "old_type": old_type}
    finally:
        conn.close()
