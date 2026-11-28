"""Load CSV / Excel files the way people actually make them.

* every sheet of a workbook is read (e.g. "Input VAT" and "Output VAT")
* the header row is found automatically, even under a report title, logo rows or blank lines
* empty rows/columns are dropped, and "Total" / "Grand total" summary rows are removed
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import List, Tuple

import pandas as pd

_HEADER_WORDS = (
    "invoice", "inv", "date", "vat", "tax", "supplier", "vendor", "customer", "client", "trn", "amount",
    "net", "total", "period", "output", "input", "debit", "credit", "account", "description", "reference",
    "status", "box", "taxable", "excl", "incl", "number", "no", "رقم", "تاريخ", "ضريبة", "المورد",
)


def _header_score(row: List[object]) -> float:
    cells = [str(c).strip().lower() for c in row if str(c).strip() and str(c).strip().lower() != "nan"]
    if len(cells) < 2:
        return 0.0
    texty = [c for c in cells if not re.fullmatch(r"[-\d.,()%\s]+", c)]
    hits = sum(1 for c in texty if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", c) for w in _HEADER_WORDS))
    return hits * 2 + len(texty) * 0.5 - (len(cells) - len(texty))


def _frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.dropna(how="all").dropna(axis=1, how="all")
    if raw.empty:
        return raw
    best_i, best_s = 0, -1.0
    for i in range(min(15, len(raw))):
        s = _header_score(list(raw.iloc[i].values))
        if s > best_s:
            best_i, best_s = i, s
    header = [str(c).strip() if str(c).strip().lower() != "nan" else f"col{j}" for j, c in enumerate(raw.iloc[best_i].values)]
    # keep header names unique
    seen: dict = {}
    for j, h in enumerate(header):
        if h in seen:
            seen[h] += 1
            header[j] = f"{h} ({seen[h]})"
        else:
            seen[h] = 0
    df = raw.iloc[best_i + 1:].copy()
    df.columns = header
    df = df.dropna(how="all")
    # drop summary rows ("Total", "Grand total", "Sub total") that sit in the first text column
    if len(df.columns):
        first = df.columns[0]
        mask = df[first].astype(str).str.strip().str.lower().str.match(r"^(grand\s+)?(sub\s*-?\s*)?totals?\b")
        df = df[~mask]
    return df.reset_index(drop=True)


def load_tables(file_bytes: bytes, filename: str, max_rows: int | None = None) -> List[Tuple[str, pd.DataFrame]]:
    """[(sheet_name, DataFrame), ...] with the real header row applied. CSV gives one sheet named 'CSV'."""
    suffix = Path(filename).suffix.lower()
    out: List[Tuple[str, pd.DataFrame]] = []
    if suffix in (".csv", ".txt"):
        for enc in ("utf-8", "utf-8-sig", "latin1"):
            try:
                raw = pd.read_csv(io.BytesIO(file_bytes), header=None, dtype=object, nrows=max_rows, encoding=enc,
                                  sep=None, engine="python")
                break
            except Exception:
                raw = None
        if raw is not None:
            out.append(("CSV", _frame_from_raw(raw)))
        return out
    try:
        book = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None, dtype=object, nrows=max_rows)
    except Exception:
        return out
    for name, raw in book.items():
        df = _frame_from_raw(raw)
        if not df.empty and len(df.columns) >= 2:
            out.append((str(name), df))
    return out


def is_sales_sheet(sheet_name: str, columns: List[str]) -> bool:
    """A sheet listing sales (output VAT) rather than purchases."""
    n = (sheet_name or "").lower()
    cols = " ".join(str(c).lower() for c in columns)
    if re.search(r"output|sales|revenue|customer", n):
        return True
    if re.search(r"input|purchase|expense|supplier", n):
        return False
    return bool(re.search(r"customer|client|buyer", cols)) and not re.search(r"supplier|vendor|seller", cols)
