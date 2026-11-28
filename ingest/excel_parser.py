import re
"""Excel and CSV tabular purchase registers and ledger parser with enhanced column matching.

Features:
- English and Arabic synonyms for all canonical VAT fields
- difflib.SequenceMatcher fuzzy matching for typo tolerance
- Value verification heuristics (TRN 15 digits starting with 100, date parsing, total ≈ taxable + VAT)
- Swapped Amount / VAT detection and automated correction (when taxable ≈ 5% of VAT)
- LLM fallback mapping with code-level verification before applying
- Per-company mapping pattern caching in database
"""
import difflib
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

import pandas as pd

from database.connection import get_db_connection


# Canonical VAT fields and ordered regex patterns (English + Arabic)
CANONICAL_COLUMNS: Dict[str, List[str]] = {
    "total": [
        r"^total$",
        r"invoice.*total",
        r"bill.*total",
        r"gross.*amount",
        r"total.*amount",
        r"total.*inc.*vat",
        r"grand.*total",
        r"المبلغ.*الإجمالي",
        r"الإجمالي",
        r"المجموع.*الكلي",
        r"المبلغ.*شاملاً.*الضريبة",
        r"المجموع",
    ],
    "taxable_amount": [
        r"taxable.*amount",
        r"taxable.*value",
        r"net.*amount",
        r"base.*amount",
        r"amount.*before.*tax",
        r"subtotal",
        r"^net$",
        r"^taxable$",
        r"المبلغ.*الخاضع.*للضريبة",
        r"القيمة.*الخاضعة.*للضريبة",
        r"المبلغ.*الصافي",
        r"الصافي",
        r"المبلغ.*قبل.*الضريبة",
    ],
    "vat_amount": [
        r"vat.*amount",
        r"tax.*amount",
        r"vat.*5%?",
        r"^vat$",
        r"input.*vat",
        r"input.*tax",
        r"مبلغ.*ضريبة.*القيمة.*المضافة",
        r"قيمة.*الضريبة",
        r"مبلغ.*الضريبة",
        r"ضريبة.*المدخلات",
        r"الضريبة",
    ],
    "invoice_number": [
        r"^inv.*(?:no|num|number|#)?$",
        r"bill.*no",
        r"doc.*no",
        r"voucher.*no",
        r"invoice.*#",
        r"inv.*#",
        r"^reference$",
        r"ref.*no",
        r"رقم.*الفاتورة",
        r"الفاتورة",
        r"رقم.*المستند",
        r"رقم.*السند",
        r"الرقم.*المرجعي",
    ],
    "date": [
        r"inv.*date",
        r"bill.*date",
        r"trans.*date",
        r"transaction.*date",
        r"^date$",
        r"entry.*date",
        r"doc.*date",
        r"posting.*date",
        r"تاريخ.*الفاتورة",
        r"تاريخ.*المستند",
        r"تاريخ.*المعاملة",
        r"التاريخ",
    ],
    "supplier_name": [
        r"vendor.*name",
        r"supplier.*name",
        r"^vendor$",
        r"^supplier$",
        r"party.*name",
        r"company.*name",
        r"seller.*name",
        r"^seller$",
        r"اسم.*المورد",
        r"المورد",
        r"اسم.*البائع",
        r"اسم.*الشركة",
        r"الطرف.*الثاني",
    ],
    "supplier_trn": [
        r"supplier.*trn",
        r"vendor.*trn",
        r"^trn$",
        r"vat.*reg.*no",
        r"tax.*no",
        r"tax.*reg.*no",
        r"الرقم.*الضريبي.*للمورد",
        r"الرقم.*الضريبي",
        r"رقم.*التسجيل.*الضريبي",
        r"رقم.*الضريبة",
    ],
    "customer_trn": [
        r"customer.*trn",
        r"client.*trn",
        r"buyer.*trn",
        r"الرقم.*الضريبي.*للعميل",
        r"رقم.*ضريبة.*العميل",
    ],
}

# Plain alias list for SequenceMatcher fuzzy scoring
CANONICAL_ALIASES: Dict[str, List[str]] = {
    "invoice_number": [
        "invoice number", "invoice no", "inv no", "inv num", "bill no", "voucher no", "doc no",
        "reference", "inv #", "invoice #", "رقم الفاتورة", "رقم المستند", "رقم السند"
    ],
    "date": [
        "invoice date", "bill date", "transaction date", "entry date", "posting date", "date",
        "تاريخ الفاتورة", "تاريخ المستند", "التاريخ"
    ],
    "taxable_amount": [
        "taxable amount", "taxable value", "net amount", "base amount", "amount before tax", "subtotal", "net",
        "المبلغ الخاضع للضريبة", "القيمة الخاضعة للضريبة", "المبلغ الصافي", "الصافي"
    ],
    "vat_amount": [
        "vat amount", "tax amount", "vat 5%", "input vat", "vat", "tax",
        "مبلغ ضريبة القيمة المضافة", "قيمة الضريبة", "مبلغ الضريبة", "الضريبة"
    ],
    "total": [
        "total", "invoice total", "bill total", "gross amount", "total amount", "grand total",
        "المبلغ الإجمالي", "الإجمالي", "المجموع الكلي", "المجموع"
    ],
    "supplier_name": [
        "supplier name", "vendor name", "supplier", "vendor", "party name", "company name",
        "اسم المورد", "المورد", "اسم البائع", "اسم الشركة"
    ],
    "supplier_trn": [
        "supplier trn", "vendor trn", "trn", "vat reg no", "tax number", "tax reg no",
        "الرقم الضريبي للمورد", "الرقم الضريبي", "رقم التسجيل الضريبي"
    ],
    "customer_trn": [
        "customer trn", "client trn", "buyer trn", "الرقم الضريبي للعميل"
    ],
}


def compute_headers_signature(headers: List[str]) -> str:
    """Compute deterministic signature from column headers."""
    clean = sorted([str(h).strip().lower() for h in headers if str(h).strip()])
    return hashlib.sha256("|".join(clean).encode("utf-8")).hexdigest()[:16]


def get_cached_column_mapping(
    business_id: int,
    headers: List[str],
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, str]]:
    """Retrieve previously confirmed column mapping for this exact header pattern."""
    sig = compute_headers_signature(headers)
    try:
        conn = get_db_connection(db_path=db_path, readonly=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT mapping_json FROM cached_column_mappings WHERE business_id = ? AND file_signature = ?;",
            (business_id, sig),
        )
        row = cursor.fetchone()
        conn.close()
        if row and row["mapping_json"]:
            return json.loads(row["mapping_json"])
    except Exception:
        pass
    return None


def save_cached_column_mapping(
    business_id: int,
    headers: List[str],
    mapping: Dict[str, str],
    db_path: Optional[Path] = None,
) -> None:
    """Cache confirmed column mapping for future automatic recognition."""
    sig = compute_headers_signature(headers)
    mapping_str = json.dumps(mapping)
    try:
        conn = get_db_connection(db_path=db_path, readonly=False)
        with conn:
            conn.execute(
                """
                INSERT INTO cached_column_mappings (business_id, file_signature, mapping_json)
                VALUES (?, ?, ?)
                ON CONFLICT(business_id, file_signature) DO UPDATE SET mapping_json = excluded.mapping_json;
                """,
                (business_id, sig, mapping_str),
            )
        conn.close()
    except Exception:
        pass


def fuzzy_match_header(header: str, canonical_field: str) -> float:
    """Return highest SequenceMatcher similarity ratio between header and known aliases."""
    clean_h = str(header).strip().lower()
    aliases = CANONICAL_ALIASES.get(canonical_field, [canonical_field])
    best = 0.0
    for alias in aliases:
        ratio = difflib.SequenceMatcher(None, clean_h, alias.lower()).ratio()
        if ratio > best:
            best = ratio
    return best


def detect_swapped_amount_and_vat(df: pd.DataFrame, taxable_col: str, vat_col: str) -> bool:
    """Detect if Amount and VAT columns were swapped.
    
    In the UAE, VAT is 5%. If the column marked 'taxable_amount' has values
    averaging approximately 5% of the column marked 'vat_amount', they are swapped!
    """
    try:
        t_vals = pd.to_numeric(df[taxable_col], errors="coerce").dropna()
        v_vals = pd.to_numeric(df[vat_col], errors="coerce").dropna()
        valid_idx = t_vals.index.intersection(v_vals.index)
        if len(valid_idx) < 2:
            return False

        t_sub = t_vals.loc[valid_idx]
        v_sub = v_vals.loc[valid_idx]
        non_zero = (t_sub > 0) & (v_sub > 0)
        if non_zero.sum() < 2:
            return False

        # Ratio of taxable / vat: normal is ~20 (since vat is 5% of taxable)
        # If swapped, taxable is ~0.05 * vat, so taxable / vat is ~0.05
        ratios = t_sub[non_zero] / v_sub[non_zero]
        median_ratio = ratios.median()
        # If median ratio is between 0.04 and 0.06, they are definitely swapped
        if 0.035 <= median_ratio <= 0.065:
            return True
        return False
    except Exception:
        return False


def verify_values_and_refine(
    df: pd.DataFrame,
    mapping: Dict[str, str],
    matched_cols: Set[str],
) -> Dict[str, str]:
    """Inspect actual sample data values to refine or correct column mapping."""
    refined = dict(mapping)

    # 1. Detect and auto-correct swapped Taxable Amount vs VAT Amount
    t_col = refined.get("taxable_amount")
    v_col = refined.get("vat_amount")
    if t_col and v_col and t_col in df.columns and v_col in df.columns:
        if detect_swapped_amount_and_vat(df, t_col, v_col):
            # Inverted! Swap them
            refined["taxable_amount"] = v_col
            refined["vat_amount"] = t_col

    # 2. Check unmapped supplier_trn with TRN value heuristic (15 digits starting with 100)
    if "supplier_trn" not in refined:
        for col in df.columns:
            if col in matched_cols:
                continue
            series_str = df[col].astype(str).str.strip().str.replace(".0", "", regex=False)
            trn_matches = series_str.str.match(r"^100\d{12}$")
            if trn_matches.mean() > 0.3:  # At least 30% valid UAE TRNs
                refined["supplier_trn"] = col
                matched_cols.add(col)
                break

    # 3. Check unmapped date column with date parsing heuristic
    if "date" not in refined:
        for col in df.columns:
            if col in matched_cols:
                continue
            try:
                sample = df[col].dropna().head(10)
                parsed = pd.to_datetime(sample, errors="coerce")
                if parsed.notna().mean() >= 0.7:
                    refined["date"] = col
                    matched_cols.add(col)
                    break
            except Exception:
                pass

    return refined


def ask_llm_column_suggestion(
    unmapped_fields: List[str],
    available_headers: List[str],
    sample_rows: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Request LLM guidance for remaining ambiguous headers and verify by code."""
    from llm_client import ask
    prompt = (
        f"You are a UAE VAT invoice register parser. Match these unmapped statutory fields: {unmapped_fields}\n"
        f"to the best matching columns from these available headers: {available_headers}.\n"
        f"Sample data rows: {sample_rows[:2]}\n"
        "Return ONLY a JSON dictionary where keys are from the unmapped statutory fields "
        "and values are the exact matching header names. E.g. {\"vat_amount\": \"Tax_5pct\"}"
    )
    try:
        resp = ask(messages=[{"role": "user", "content": prompt}], tools=None)
        txt = resp.content.strip()
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            suggestion = json.loads(m.group(0))
            # Code verification: ensure suggested headers actually exist!
            verified: Dict[str, str] = {}
            for k, v in suggestion.items():
                if k in unmapped_fields and v in available_headers:
                    verified[k] = v
            return verified
    except Exception:
        pass
    return {}


def guess_column_mapping(
    columns: List[str],
    df: Optional[pd.DataFrame] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
    use_llm_fallback: bool = False,
) -> Tuple[Dict[str, str], float, List[str]]:
    """Guess column mapping using regex, fuzzy SequenceMatcher, data heuristics, and cached patterns.
    
    Returns (mapping, confidence, unmapped).
    """
    clean_cols = [str(c).strip() for c in columns if str(c).strip()]
    if not clean_cols:
        return {}, 0.0, ["invoice_number", "date", "taxable_amount", "vat_amount"]

    # 1. Check cached mapping from database if business_id provided
    if business_id is not None:
        cached = get_cached_column_mapping(business_id=business_id, headers=clean_cols, db_path=db_path)
        if cached:
            # Verify cached columns exist in this file
            if all(v in clean_cols for v in cached.values()):
                reqs = ["invoice_number", "date", "taxable_amount", "vat_amount"]
                unmapped = [r for r in reqs if r not in cached]
                return cached, 1.0 if not unmapped else 0.9, unmapped

    mapping: Dict[str, str] = {}
    matched_actual: Set[str] = set()

    # 2. Stage 1: Exact / Regex pattern matching (English + Arabic)
    for canonical, patterns in CANONICAL_COLUMNS.items():
        for actual in clean_cols:
            if actual in matched_actual:
                continue
            clean_actual = actual.lower()
            if any(re.search(pat, clean_actual, re.IGNORECASE) for pat in patterns):
                mapping[canonical] = actual
                matched_actual.add(actual)
                break

    # 3. Stage 2: Fuzzy string matching using difflib.SequenceMatcher (threshold 0.70)
    for canonical in ["invoice_number", "date", "taxable_amount", "vat_amount", "total", "supplier_name", "supplier_trn"]:
        if canonical not in mapping:
            best_score = 0.0
            best_col = None
            for actual in clean_cols:
                if actual in matched_actual:
                    continue
                score = fuzzy_match_header(actual, canonical)
                if score >= 0.70 and score > best_score:
                    best_score = score
                    best_col = actual
            if best_col:
                mapping[canonical] = best_col
                matched_actual.add(best_col)

    # 4. Stage 3: Data value verification and swapped amount/VAT heuristic
    if df is not None and not df.empty:
        mapping = verify_values_and_refine(df, mapping, matched_actual)

    # Required fields calculation
    required_fields = ["invoice_number", "date", "taxable_amount", "vat_amount"]
    matched_required = sum(1 for req in required_fields if req in mapping)
    confidence = round(matched_required / len(required_fields), 2)
    unmapped = [req for req in required_fields if req not in mapping]

    # 5. Stage 4: LLM fallback if confidence < 0.8 and LLM enabled
    if unmapped and confidence < 0.8 and use_llm_fallback:
        avail_headers = [c for c in clean_cols if c not in mapping.values()]
        samples = df.head(3).to_dict(orient="records") if df is not None else []
        llm_sug = ask_llm_column_suggestion(unmapped, avail_headers, samples)
        for k, v in llm_sug.items():
            mapping[k] = v
        # Recompute confidence
        matched_required = sum(1 for req in required_fields if req in mapping)
        confidence = round(matched_required / len(required_fields), 2)
        unmapped = [req for req in required_fields if req not in mapping]

    return mapping, confidence, unmapped


def parse_register_file(
    file_path: Path,
    custom_mapping: Optional[Dict[str, str]] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Parse CSV or XLSX file and normalize rows into structured records."""
    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Register file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(path)
    elif suffix in [".csv", ".txt"]:
        try:
            df = pd.read_csv(path)
        except Exception:
            df = pd.read_csv(path, encoding="latin1")
    else:
        raise ValueError(f"Unsupported spreadsheet format: {suffix}")

    df.columns = [str(c).strip() for c in df.columns]
    mapping, confidence, unmapped = guess_column_mapping(
        columns=list(df.columns),
        df=df,
        business_id=business_id,
        db_path=db_path,
    )
    if custom_mapping:
        mapping.update(custom_mapping)
        unmapped = [req for req in ["invoice_number", "date", "taxable_amount", "vat_amount"] if req not in mapping]
        confidence = 1.0 if not unmapped else round((4 - len(unmapped)) / 4, 2)

    requires_confirmation = confidence < 0.8

    records: List[Dict[str, Any]] = []
    inv_col = mapping.get("invoice_number")
    date_col = mapping.get("date")
    taxable_col = mapping.get("taxable_amount")
    vat_col = mapping.get("vat_amount")
    total_col = mapping.get("total")
    supplier_col = mapping.get("supplier_name")
    trn_col = mapping.get("supplier_trn")

    for _, row in df.iterrows():
        taxable = float(row.get(taxable_col, 0.0)) if taxable_col and pd.notna(row.get(taxable_col)) else 0.0
        vat = float(row.get(vat_col, 0.0)) if vat_col and pd.notna(row.get(vat_col)) else 0.0
        total = float(row.get(total_col, 0.0)) if total_col and pd.notna(row.get(total_col)) else round(taxable + vat, 2)

        record = {
            "invoice_number": str(row.get(inv_col, "")).strip() if inv_col else "",
            "date": str(row.get(date_col, "")).strip() if date_col else "",
            "supplier_name": str(row.get(supplier_col, "")).strip() if supplier_col else "Unknown Supplier",
            "supplier_trn": str(row.get(trn_col, "")).strip() if trn_col and pd.notna(row.get(trn_col)) else None,
            "taxable_amount": round(taxable, 2),
            "vat_amount": round(vat, 2),
            "total": round(total, 2),
            "currency": "AED",
        }
        records.append(record)

    return {
        "records": records,
        "mapping": mapping,
        "confidence": confidence,
        "requires_confirmation": requires_confirmation,
        "unmapped": unmapped,
        "total_rows": len(records),
    }


def parse_excel_register(
    file_bytes: bytes,
    filename: str,
    custom_mapping: Optional[Dict[str, str]] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse an invoice register (CSV or Excel, every sheet) and return (records, unmapped_fields).

    * the header row is found automatically on every sheet
    * a sheet of sales ("Output VAT", "Sales", customer columns) gives direction='sales'
    * dates are normalised to ISO (day-first, as in the UAE), amounts like "1,250.00" are read
    * rows without an invoice number and amounts (blank lines, totals, notes) are skipped
    """
    from ingest.table_loader import load_tables, is_sales_sheet
    from ingest.invoice_reader import parse_date, to_number
    from ingest.doc_classifier import _classify_columns

    sheets = load_tables(file_bytes, filename)
    records: List[Dict[str, Any]] = []
    all_unmapped: List[str] = []
    usable = [(n, df) for n, df in sheets if _classify_columns(list(df.columns), len(df))["type"] == "register"]
    if not usable and sheets:
        usable = sheets[:1]
    for sheet_name, df in usable:
        df.columns = [str(c).strip() for c in df.columns]
        mapping, confidence, unmapped = guess_column_mapping(columns=list(df.columns), df=df,
                                                             business_id=business_id, db_path=db_path)
        if custom_mapping:
            mapping.update({k: v for k, v in custom_mapping.items() if v in df.columns})
            unmapped = [r for r in ["invoice_number", "date", "taxable_amount", "vat_amount"] if r not in mapping]
        all_unmapped += [u for u in unmapped if u not in all_unmapped]
        sales = is_sales_sheet(sheet_name, list(df.columns))
        cols_low = {c.lower(): c for c in df.columns}
        party_col = mapping.get("supplier_name")
        party_trn = mapping.get("supplier_trn")
        if sales:
            party_col = next((cols_low[c] for c in cols_low if re.search(r"customer|client|buyer", c) and "trn" not in c), party_col)
            party_trn = next((cols_low[c] for c in cols_low if re.search(r"(customer|client|buyer).*trn|trn.*(customer|client)", c)), party_trn)

        def cell(row, colname):
            if not colname or colname not in row.index:
                return None
            v = row[colname]
            return None if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip().lower() in ("", "nan", "nat") else v

        for _, row in df.iterrows():
            inv_no = cell(row, mapping.get("invoice_number"))
            taxable = to_number(cell(row, mapping.get("taxable_amount")))
            vat = to_number(cell(row, mapping.get("vat_amount")))
            total = to_number(cell(row, mapping.get("total")))
            if inv_no is None or (taxable is None and vat is None and total is None):
                continue
            if re.match(r"(?i)^(grand\s+)?(sub\s*-?\s*)?totals?\b", str(inv_no).strip()):
                continue
            if taxable is None and total is not None and vat is not None:
                taxable = total - vat
            if vat is None and taxable is not None and total is not None:
                vat = total - taxable
            taxable, vat = abs(taxable or 0.0), abs(vat or 0.0)
            total = abs(total) if total is not None else round(taxable + vat, 2)
            raw_date = cell(row, mapping.get("date"))
            if hasattr(raw_date, "date") and not isinstance(raw_date, str):
                d_iso = raw_date.date().isoformat() if hasattr(raw_date, "hour") else raw_date.isoformat()
            else:
                d_iso = parse_date(str(raw_date)) if raw_date is not None else None
            party = cell(row, party_col)
            trn = cell(row, party_trn)
            trn = str(trn).split(".")[0].strip() if trn is not None else None
            records.append({
                "invoice_number": str(inv_no).strip(),
                "date": d_iso or "",
                "supplier_name": (str(party).strip() if party is not None else ("Customer" if sales else "Unknown Supplier")),
                "supplier_trn": trn,
                "taxable_amount": round(taxable, 2),
                "vat_amount": round(vat, 2),
                "total": round(total, 2),
                "currency": "AED",
                "direction": "sales" if sales else "purchase",
                "description_lines": [f"From sheet '{sheet_name}'" if len(sheets) > 1 else "Commercial purchase"],
            })
    return records, all_unmapped
