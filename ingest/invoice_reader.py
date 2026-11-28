"""Rule-based tax-invoice reader (no AI needed).

Reads the fields the FTA requires on a tax invoice from the document text
(text PDF, scanned PDF or photo after OCR):

* invoice number and date (DD/MM/YYYY, 12-Jan-2026, 2026-01-12, 12.01.2026, January 12, 2026 ...)
* seller and buyer, each with its TRN
* net amount, VAT and total, read from their labels and cross-checked (net + VAT = total)
* direction: a *sale* when the seller is your own company (your TRN or name),
  otherwise a *purchase*

Every field carries how it was found, and a confidence score is worked out from
which fields were found and whether the amounts agree.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

TRN_RE = re.compile(r"(?<!\d)(100\d{12})(?!\d)")
_NUM = r"\(?-?\d{1,3}(?:[,.\s]\d{3})+(?:[.,]\d{1,2})?\)?|\(?-?\d+(?:[.,]\d{1,2})?\)?"
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_DATE_PATTERNS = [
    # 12/01/2026, 12-01-2026, 12.01.2026 (UAE day-first)
    (re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})\b"), "dmy"),
    # 2026-01-12, 2026/01/12
    (re.compile(r"\b(20\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})\b"), "ymd"),
    # 12 Jan 2026, 12-Jan-2026, 12 January, 2026
    (re.compile(r"\b(\d{1,2})[\s\-]+([A-Za-z]{3,9})[\s\-,]+(20\d{2})\b"), "d_mon_y"),
    # January 12, 2026 / Jan 12 2026
    (re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(20\d{2})\b"), "mon_d_y"),
]


def to_number(s: Any) -> Optional[float]:
    if s is None:
        return None
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = re.sub(r"[^\d.,]", "", t.replace(" ", ""))
    # Decimal mark = the last '.' or ',' followed by 1-2 digits; every other separator is thousands.
    m = re.search(r"[.,](\d{1,2})$", t)
    if m:
        t = re.sub(r"[.,]", "", t[: m.start()]) + "." + m.group(1)
    else:
        t = re.sub(r"[.,]", "", t)
    if not t or t == ".":
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(text: str) -> Optional[str]:
    """First valid date in `text` as ISO (YYYY-MM-DD), or None."""
    hits: List[Tuple[int, str]] = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            try:
                if kind == "dmy":
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                elif kind == "ymd":
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "d_mon_y":
                    mon = _MONTHS.get(m.group(2)[:3].lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(1)))
                else:
                    mon = _MONTHS.get(m.group(1)[:3].lower())
                    if not mon:
                        continue
                    d = date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                continue
            hits.append((m.start(), d.isoformat()))
    return sorted(hits)[0][1] if hits else None


def _lines(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _value_after(lines: List[str], labels: List[str], kind: str = "text", lookahead: int = 1) -> Optional[str]:
    """The value written after a label on the same line (or on the next line)."""
    for lab in labels:  # most specific label first
        for i, ln in enumerate(lines):
            m = re.search(lab + r"\s*[:#\-]?\s*(.*)$", ln, re.I)
            if not m:
                continue
            if kind == "money" and _skip_for(lab, ln):
                continue
            rest = m.group(1).strip()
            cands = [rest] + lines[i + 1: i + 1 + lookahead]
            for c in cands:
                if not c:
                    continue
                if kind == "date":
                    d = parse_date(c)
                    if d:
                        return d
                elif kind == "money":
                    nums = re.findall(_NUM, c)
                    nums = [n for n in nums if re.search(r"\d", n)]
                    # pick the last number on the line (amount column), skip '5' of 'VAT 5%'
                    vals = [to_number(n) for n in nums]
                    vals = [v for v in vals if v is not None and not (re.search(r"\b5\s*%", c) and v == 5)]
                    if vals:
                        return str(vals[-1])
                elif kind == "id":
                    mm = re.search(r"([A-Z0-9][A-Z0-9\-/_.]{2,30})", c, re.I)
                    if mm and not re.fullmatch(r"(?i)date|no|number|invoice", mm.group(1)):
                        return mm.group(1).rstrip(".")
                else:
                    c2 = re.sub(r"\s*TRN\b.*$", "", c, flags=re.I).strip(" :-|")
                    if c2 and not re.fullmatch(r"[\d\s.,/:-]+", c2):
                        return c2[:120]
    return None


def _skip_for(label: str, line: str) -> bool:
    """A bare 'VAT' label must not pick up 'Subtotal (excl. VAT)' or 'Total incl. VAT' lines."""
    if label in (r"\bvat\b(?!\s*(?:reg|no|number|trn))", r"tax\s+amount"):
        return bool(re.search(r"(?i)sub\s*-?\s*total|excl|before\s+vat|incl|including|grand|total\s*\(|amount\s+due", line))
    return False


def _norm_name(s: Optional[str]) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(demo\)|\b(llc|l\.l\.c|fze|fzco|fz-llc|est|establishment|trading|co|company|ltd|limited|pjsc)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _same_company(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _norm_name(a), _norm_name(b)
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


def read_invoice(text: str, filename: str = "", own_trn: Optional[str] = None,
                 own_name: Optional[str] = None) -> Dict[str, Any]:
    """Read a tax invoice. Returns a dict with the invoice fields plus 'confidence' and 'notes'."""
    lines = _lines(text)
    low = (text or "").lower()
    notes: List[str] = []

    # ---------- number ----------
    number = _value_after(lines, [r"tax\s+invoice\s+(?:no\.?|number|#)", r"invoice\s*(?:no\.?|number|#|num)",
                                  r"inv\s*(?:no\.?|#)", r"bill\s*(?:no\.?|number|#)", r"document\s*(?:no\.?|number)",
                                  r"رقم\s*الفاتورة"], kind="id")
    if not number:
        m = re.search(r"\b((?:INV|PUR|SAL|SI|PI|TI|AN|BILL)[-/]?\d[\w\-/]{1,20})\b", text or "", re.I) or \
            re.search(r"\b((?:INV|PUR|SAL|SI|PI|TI|BILL)[-/]?\w*\d[\w\-/]*)\b", filename or "", re.I)
        number = m.group(1) if m else None
    if not number:
        notes.append("invoice number not found")

    # ---------- date ----------
    inv_date = _value_after(lines, [r"invoice\s+date", r"date\s+of\s+(?:issue|invoice|supply)", r"tax\s+point",
                                    r"issue\s+date", r"\bdated?\b", r"تاريخ"], kind="date")
    if not inv_date:
        inv_date = parse_date(text)
        if inv_date:
            notes.append("date taken from the first date in the document")
    if not inv_date:
        notes.append("invoice date not found")

    # ---------- parties + TRNs ----------
    trns = TRN_RE.findall(text or "")
    seller = _value_after(lines, [r"(?:supplier|seller|vendor|sold\s+by|bill(?:ed)?\s+from|from)\s*(?:name)?"])
    buyer = _value_after(lines, [r"(?:customer|buyer|client|bill(?:ed)?\s+to|sold\s+to|invoice\s+to|ship\s+to)\s*(?:name)?"])
    if not seller:
        # Letterhead: first line that looks like a company name, before the buyer block.
        for ln in lines[:8]:
            if re.search(r"\b(llc|l\.l\.c|fze|fzco|est\.?|trading|ltd|limited|company|group|pjsc)\b", ln, re.I) \
                    and not re.search(r"bill|customer|buyer|to\s*:", ln, re.I):
                seller = re.sub(r"\s*TRN.*$", "", ln, flags=re.I).strip(" :-")[:120]
                break

    def trn_near(label_rx: str) -> Optional[str]:
        for i, ln in enumerate(lines):
            if re.search(label_rx, ln, re.I):
                for c in lines[i: i + 4]:
                    m = TRN_RE.search(c)
                    if m:
                        return m.group(1)
        return None

    buyer_trn = trn_near(r"customer|buyer|bill(?:ed)?\s+to|sold\s+to|client|invoice\s+to|customer\s+trn")
    seller_trn = trn_near(r"supplier|seller|vendor|sold\s+by|from\b")
    if not seller_trn:
        others = [t for t in trns if t != buyer_trn]
        seller_trn = others[0] if others else None
    if buyer_trn == seller_trn and len(set(trns)) > 1:
        buyer_trn = next((t for t in trns if t != seller_trn), buyer_trn)
    if not seller_trn:
        notes.append("supplier TRN not found")

    # ---------- direction ----------
    seller_is_me = bool(own_trn) and seller_trn == own_trn
    buyer_is_me = bool(own_trn) and buyer_trn == own_trn
    if seller_is_me and not buyer_is_me:
        direction = "sales"
    elif buyer_is_me:
        direction = "purchase"
    elif _same_company(seller, own_name) and not _same_company(buyer, own_name):
        direction = "sales"
    elif _same_company(buyer, own_name):
        direction = "purchase"
    elif re.search(r"(?i)\bsales?[_\s-]*invoice|\bSAL-", filename or ""):
        direction = "sales"; notes.append("sale/purchase taken from the file name")
    else:
        direction = "purchase"
        if own_trn and own_trn not in trns:
            notes.append("your TRN is not on this invoice")

    # ---------- amounts ----------
    net = to_number(_value_after(lines, [r"sub\s*-?\s*total(?:\s*\(?excl\.?\s*vat\)?)?", r"total\s+(?:excl\.?|excluding|before)\s+vat",
                                         r"taxable\s+(?:amount|value|supplies)", r"net\s+(?:amount|total|value)",
                                         r"amount\s+(?:excl\.?|excluding|before)\s+vat", r"المجموع\s*الفرعي"], kind="money"))
    vat = to_number(_value_after(lines, [r"total\s+vat", r"vat\s+amount", r"vat\s*@?\s*5\s*%", r"vat\s*\(5\s*%\)", r"\bvat\b(?!\s*(?:reg|no|number|trn))",
                                         r"tax\s+amount", r"ضريبة\s*القيمة\s*المضافة"], kind="money"))
    total = to_number(_value_after(lines, [r"(?:grand\s+)?total\s*\(?(?:incl\.?|including|inc\.?)\s*vat\)?", r"grand\s+total",
                                           r"total\s+amount\s+(?:due|payable)", r"amount\s+(?:due|payable)", r"invoice\s+total",
                                           r"total\s+due", r"^total\b(?!\s*(?:excl|vat|before))", r"الإجمالي"], kind="money"))
    # Fill in a missing amount from the other two; prefer what is written.
    if net is not None and vat is not None and total is None:
        total = round(net + vat, 2)
    elif net is not None and total is not None and vat is None:
        vat = round(total - net, 2)
    elif vat is not None and total is not None and net is None:
        net = round(total - vat, 2)
    elif total is not None and net is None and vat is None:
        net = round(total / 1.05, 2); vat = round(total - net, 2)
        notes.append("net and VAT worked out from the total at 5%")
    amounts_ok = None not in (net, vat, total) and abs((net or 0) + (vat or 0) - (total or 0)) <= max(1.0, 0.005 * (total or 0))
    if None in (net, vat, total):
        notes.append("amounts not found")
    elif not amounts_ok:
        notes.append(f"net + VAT ({net + vat:,.2f}) does not equal the total ({total:,.2f})")
    if vat and net and net > 0 and not (0.045 <= vat / net <= 0.055) and vat / net > 0.001:
        notes.append(f"VAT is {vat / net:.1%} of the net amount, not 5%")

    # ---------- confidence ----------
    score = 0.0
    score += 0.15 if number else 0
    score += 0.2 if inv_date else 0
    score += 0.15 if seller_trn else 0
    score += 0.1 if ("tax invoice" in low or "فاتورة ضريبية" in low) else 0
    score += 0.3 if amounts_ok else (0.1 if vat else 0)
    score += 0.1 if (own_trn and own_trn in trns) else 0.05
    return {
        "direction": direction,
        "invoice_number": number,
        "date": inv_date,
        "supplier_name": seller,
        "supplier_trn": seller_trn,
        "customer_name": buyer,
        "customer_trn": buyer_trn,
        "taxable_amount": net,
        "vat_amount": vat,
        "total": total,
        "currency": "AED",
        "confidence": round(min(score, 1.0), 2),
        "amounts_agree": amounts_ok,
        "notes": notes,
    }


def business_identity(business_id: Optional[int], db_path=None) -> Tuple[Optional[str], Optional[str]]:
    """(TRN, name) of the business, used to tell sales from purchases."""
    if not business_id:
        return None, None
    try:
        from database.connection import get_db_connection
        conn = get_db_connection(db_path=db_path, readonly=True)
        try:
            row = conn.execute("SELECT trn, name FROM businesses WHERE id = ?;", (business_id,)).fetchone()
        finally:
            conn.close()
        return (row["trn"], row["name"]) if row else (None, None)
    except Exception:
        return None, None
