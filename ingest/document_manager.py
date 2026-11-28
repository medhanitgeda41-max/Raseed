"""Cloud document manager: list, search, include/exclude from analysis, and delete.

Every uploaded document keeps the rows Raseed extracted from it (invoices, VAT
returns, ledger lines). When a document is *excluded* from the analysis those
rows are set aside in `parked_rows`, so every calculation (credits, deadlines,
reconciliation, refund packs) automatically ignores them. Including it again
puts the rows back exactly as they were, including any manual corrections.
Deleting a document removes the file from cloud storage and all of its data.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.connection import get_db_connection

EXTRACTED_TABLES = ("invoices", "vat_returns", "ledger_entries")


def list_cloud_documents(business_id: int, search: str = "", db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """All stored documents for a business, newest first, optionally filtered by name."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, original_filename, document_type, file_size_bytes, uploaded_at, storage_path, "
            "COALESCE(included_in_analysis, 1) AS included_in_analysis "
            "FROM uploaded_documents WHERE business_id = ? ORDER BY id DESC;",
            (business_id,),
        )
        docs = [dict(r) for r in cur.fetchall()]
        counts: Dict[int, int] = {}
        for tbl in EXTRACTED_TABLES:
            cur.execute(
                f"SELECT document_id, COUNT(*) AS c FROM {tbl} WHERE business_id = ? AND document_id IS NOT NULL "
                "GROUP BY document_id;",
                (business_id,),
            )
            for r in cur.fetchall():
                counts[r["document_id"]] = counts.get(r["document_id"], 0) + int(r["c"])
        cur.execute(
            "SELECT document_id, COUNT(*) AS c FROM parked_rows WHERE business_id = ? GROUP BY document_id;",
            (business_id,),
        )
        parked = {r["document_id"]: int(r["c"]) for r in cur.fetchall()}
    finally:
        conn.close()

    term = (search or "").strip().lower()
    out = []
    for d in docs:
        if term and term not in str(d["original_filename"]).lower() and term not in str(d["document_type"]).lower():
            continue
        d["included_in_analysis"] = bool(d["included_in_analysis"])
        d["records"] = counts.get(d["id"], 0) + parked.get(d["id"], 0)
        out.append(d)
    return out


def _park(conn, business_id: int, document_id: int) -> int:
    cur = conn.cursor()
    moved = 0
    # Ledger lines may point at invoices we are about to park: unlink first.
    conn.execute(
        "UPDATE ledger_entries SET reconciled_invoice_id = NULL WHERE business_id = ? AND reconciled_invoice_id IN "
        "(SELECT id FROM invoices WHERE business_id = ? AND document_id = ?);",
        (business_id, business_id, document_id),
    )
    for tbl in EXTRACTED_TABLES:
        cur.execute(f"SELECT * FROM {tbl} WHERE business_id = ? AND document_id = ?;", (business_id, document_id))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r.pop("id", None)
            if tbl == "ledger_entries":
                r["reconciled_invoice_id"] = None
            conn.execute(
                "INSERT INTO parked_rows (business_id, document_id, table_name, row_json) VALUES (?, ?, ?, ?);",
                (business_id, document_id, tbl, json.dumps(r, default=str)),
            )
            moved += 1
        conn.execute(f"DELETE FROM {tbl} WHERE business_id = ? AND document_id = ?;", (business_id, document_id))
    conn.execute("UPDATE uploaded_documents SET included_in_analysis = 0 WHERE id = ? AND business_id = ?;",
                 (document_id, business_id))
    return moved


def _unpark(conn, business_id: int, document_id: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id, table_name, row_json FROM parked_rows WHERE business_id = ? AND document_id = ? ORDER BY id;",
                (business_id, document_id))
    parked = [dict(r) for r in cur.fetchall()]
    restored = 0
    for p in parked:
        tbl = p["table_name"]
        if tbl not in EXTRACTED_TABLES:
            continue
        row = json.loads(p["row_json"])
        row.pop("id", None)
        cols = [c for c in row.keys()]
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES ({placeholders})"
        if tbl == "vat_returns":
            sql += " ON CONFLICT(business_id, period) DO NOTHING"
        conn.execute(sql + ";", tuple(row[c] for c in cols))
        restored += 1
    conn.execute("DELETE FROM parked_rows WHERE business_id = ? AND document_id = ?;", (business_id, document_id))
    conn.execute("UPDATE uploaded_documents SET included_in_analysis = 1 WHERE id = ? AND business_id = ?;",
                 (document_id, business_id))
    return restored


def set_analysis_selection(business_id: int, included_ids: Iterable[int],
                           db_path: Optional[Path] = None) -> Dict[str, int]:
    """Make exactly `included_ids` count in the analysis; every other document is set aside."""
    wanted = {int(i) for i in included_ids}
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, COALESCE(included_in_analysis, 1) AS inc FROM uploaded_documents WHERE business_id = ?;",
                    (business_id,))
        current = {int(r["id"]): bool(r["inc"]) for r in cur.fetchall()}
        result = {"included": 0, "excluded": 0}
        with conn:
            for doc_id, inc in current.items():
                if doc_id in wanted and not inc:
                    _unpark(conn, business_id, doc_id)
                    result["included"] += 1
                elif doc_id not in wanted and inc:
                    _park(conn, business_id, doc_id)
                    result["excluded"] += 1
        return result
    finally:
        conn.close()


def delete_cloud_documents(business_id: int, document_ids: Iterable[int],
                           db_path: Optional[Path] = None) -> int:
    """Delete documents: the stored file (Cloudflare R2 or local) and all data read from it."""
    ids = [int(i) for i in document_ids]
    if not ids:
        return 0
    conn = get_db_connection(db_path=db_path, readonly=False)
    paths: List[str] = []
    deleted = 0
    try:
        cur = conn.cursor()
        with conn:
            for doc_id in ids:
                cur.execute("SELECT storage_path FROM uploaded_documents WHERE id = ? AND business_id = ?;",
                            (doc_id, business_id))
                row = cur.fetchone()
                if not row:
                    continue
                paths.append(row["storage_path"])
                conn.execute(
                    "UPDATE ledger_entries SET reconciled_invoice_id = NULL WHERE business_id = ? AND reconciled_invoice_id IN "
                    "(SELECT id FROM invoices WHERE business_id = ? AND document_id = ?);",
                    (business_id, business_id, doc_id),
                )
                for tbl in EXTRACTED_TABLES:
                    conn.execute(f"DELETE FROM {tbl} WHERE business_id = ? AND document_id = ?;", (business_id, doc_id))
                conn.execute("DELETE FROM parked_rows WHERE business_id = ? AND document_id = ?;", (business_id, doc_id))
                conn.execute("DELETE FROM uploaded_documents WHERE id = ? AND business_id = ?;", (doc_id, business_id))
                deleted += 1
            conn.execute(
                "INSERT INTO audit_events (business_id, actor, event_type, details_json) VALUES (?, 'USER', ?, ?);",
                (business_id, "DOCUMENTS_DELETED", json.dumps({"document_ids": ids, "count": deleted})),
            )
    finally:
        conn.close()

    try:
        from ingest.storage_interface import get_storage_backend
        backend = get_storage_backend()
        for p in paths:
            try:
                backend.delete_file(p)
            except Exception:
                pass
    except Exception:
        pass
    return deleted


def _doc_data_ranges(business_id: int, db_path: Optional[Path] = None) -> Dict[int, Dict[str, str]]:
    """Earliest and latest business date found inside each document (invoices, returns, ledger)."""
    ranges: Dict[int, Dict[str, str]] = {}

    def _add(doc_id: Any, lo: Any, hi: Any) -> None:
        if doc_id is None or not lo:
            return
        lo, hi = str(lo)[:10], str(hi or lo)[:10]
        r = ranges.setdefault(int(doc_id), {"from": lo, "to": hi})
        r["from"], r["to"] = min(r["from"], lo), max(r["to"], hi)

    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        cur = conn.cursor()
        for sql in (
            "SELECT document_id, MIN(date) AS lo, MAX(date) AS hi FROM invoices WHERE business_id = ? GROUP BY document_id;",
            "SELECT document_id, MIN(period_start) AS lo, MAX(period_end) AS hi FROM vat_returns WHERE business_id = ? GROUP BY document_id;",
            "SELECT document_id, MIN(entry_date) AS lo, MAX(entry_date) AS hi FROM ledger_entries WHERE business_id = ? GROUP BY document_id;",
        ):
            cur.execute(sql, (business_id,))
            for r in cur.fetchall():
                _add(r["document_id"], r["lo"], r["hi"])
        cur.execute("SELECT document_id, table_name, row_json FROM parked_rows WHERE business_id = ?;", (business_id,))
        for r in cur.fetchall():
            try:
                row = json.loads(r["row_json"])
            except Exception:
                continue
            if r["table_name"] == "vat_returns":
                _add(r["document_id"], row.get("period_start"), row.get("period_end"))
            else:
                d = row.get("date") or row.get("entry_date")
                _add(r["document_id"], d, d)
    finally:
        conn.close()
    return ranges


def find_documents(
    business_id: int,
    uploaded_from: Optional[str] = None,
    uploaded_to: Optional[str] = None,
    covers_from: Optional[str] = None,
    covers_to: Optional[str] = None,
    search: str = "",
    document_type: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Find uploaded documents by upload date, by the business dates they cover, by name or type.

    Dates are ISO 'YYYY-MM-DD' and inclusive. `covers_*` matches documents whose
    invoices / VAT return periods / ledger lines overlap that date range.
    """
    docs = list_cloud_documents(business_id, search=search, db_path=db_path)
    ranges = _doc_data_ranges(business_id, db_path=db_path)
    out = []
    for d in docs:
        up = str(d.get("uploaded_at") or "")[:10]
        if uploaded_from and up and up < uploaded_from[:10]:
            continue
        if uploaded_to and up and up > uploaded_to[:10]:
            continue
        if document_type and d["document_type"] != document_type:
            continue
        rng = ranges.get(d["id"])
        if covers_from or covers_to:
            if not rng:
                continue
            if covers_from and rng["to"] < covers_from[:10]:
                continue
            if covers_to and rng["from"] > covers_to[:10]:
                continue
        out.append({
            "file_name": d["original_filename"],
            "type": d["document_type"],
            "uploaded_on": str(d.get("uploaded_at") or "")[:16],
            "covers_dates": f"{rng['from']} to {rng['to']}" if rng else "no dated records",
            "records": d["records"],
            "used_in_analysis": d["included_in_analysis"],
        })
    return {"count": len(out), "documents": out}


def document_inventory_text(business_id: int, limit: int = 80, db_path: Optional[Path] = None) -> str:
    """One line per stored file, given to the AI every turn so it always sees every file."""
    from ingest.doc_classifier import DOC_TYPE_LABELS
    try:
        from tools.compute_tools import document_problems
        probs = document_problems(business_id, db_path=db_path)
    except Exception:
        probs = {}
    found = find_documents(business_id, db_path=db_path)["documents"]
    docs = list_cloud_documents(business_id, db_path=db_path)
    by_name_id = {d["original_filename"]: d["id"] for d in docs}
    if not found:
        return "  (no files uploaded yet)"
    lines = []
    for i, d in enumerate(found[:limit], 1):
        doc_id = by_name_id.get(d["file_name"])
        p = probs.get(doc_id, {}).get("problems", []) if doc_id else []
        lines.append(
            f"  {i}. {d['file_name']} | {DOC_TYPE_LABELS.get(d['type'], d['type'])} | {d['records']} record(s) | "
            f"covers {d['covers_dates']} | uploaded {d['uploaded_on'][:10]} | "
            f"{'used in analysis' if d['used_in_analysis'] else 'NOT used (unticked)'} | "
            f"{len(p)} problem(s)" + (f": {'; '.join(p[:3])}" + (' …' if len(p) > 3 else '') if p else "")
        )
    if len(found) > limit:
        lines.append(f"  …and {len(found) - limit} more file(s); use find_my_documents to search them.")
    return "\n".join(lines)
