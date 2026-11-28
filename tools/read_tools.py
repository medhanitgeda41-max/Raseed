"""Safe, read-only analytical queries and inspection tools for Raseed."""
from database.request_cache import request_cached
from pathlib import Path
from typing import Dict, Any, List, Optional
from database.connection import get_db_connection, get_or_create_default_business
from onboarding.requirements import onboarding_status
from guards.deadline_engine import calculate_credit_expiry
from datetime import date


@request_cached
def get_business_profile(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve the registered business profile for a specific business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM business_profile WHERE business_id = ? ORDER BY id DESC LIMIT 1;", (biz_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_current_onboarding_status(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Retrieve onboarding checklist completeness and readiness for a business."""
    return onboarding_status(business_id=business_id, db_path=db_path)


@request_cached
def list_documents(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List all locally stored documents belonging to a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM uploaded_documents WHERE business_id = ? ORDER BY id DESC;", (biz_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


@request_cached
def get_invoices(
    filters: Optional[Dict[str, Any]] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Query invoices for a business with optional filters."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM invoices WHERE business_id = ?"
        params: List[Any] = [biz_id]

        if filters:
            if "direction" in filters:
                query += " AND direction = ?"
                params.append(filters["direction"])
            if "eligibility_status" in filters:
                query += " AND eligibility_status = ?"
                params.append(filters["eligibility_status"])
            if "date_from" in filters:
                query += " AND date >= ?"
                params.append(filters["date_from"])
            if "date_to" in filters:
                query += " AND date <= ?"
                params.append(filters["date_to"])

        query += " ORDER BY date DESC, id DESC;"
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


@request_cached
def get_vat_returns(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve filed Form 201 VAT returns for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vat_returns WHERE business_id = ? ORDER BY period_end DESC;", (biz_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


@request_cached
def get_credit_ledger(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve the excess recoverable input tax ledger for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM credit_ledger WHERE business_id = ? ORDER BY period_end_date ASC;", (biz_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


@request_cached
def get_deadlines(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve statutory deadlines for all active excess credit periods for a business."""
    rows = get_credit_ledger(business_id=business_id, db_path=db_path)
    deadlines: List[Dict[str, Any]] = []
    for r in rows:
        end_d = date.fromisoformat(r["period_end_date"])
        d = calculate_credit_expiry(period_code=r["period_code"], period_end_date=end_d)
        deadlines.append(d.model_dump())
    return deadlines


@request_cached
def get_document_requests(
    status: Optional[str] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve document requests for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM document_requests WHERE business_id = ? AND status = ? ORDER BY id DESC;",
                (biz_id, status),
            )
        else:
            cursor.execute("SELECT * FROM document_requests WHERE business_id = ? ORDER BY id DESC;", (biz_id,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


@request_cached
def get_refund_status(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Retrieve summary of historical and active refund packages for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM refund_records WHERE business_id = ? ORDER BY id DESC;", (biz_id,))
        records = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            """
            SELECT SUM(net_excess_aed) as total_excess,
                   SUM(CASE WHEN status IN ('URGENT', 'DUE_SOON') THEN net_excess_aed ELSE 0 END) as at_risk
            FROM credit_ledger WHERE business_id = ? AND status != 'EXPIRED';
            """,
            (biz_id,),
        )
        totals = cursor.fetchone()

        return {
            "records": records,
            "total_available_credits_aed": totals["total_excess"] or 0.0,
            "at_risk_credits_aed": totals["at_risk"] or 0.0,
        }
    finally:
        conn.close()


@request_cached
def get_staged_actions(
    status: Optional[str] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Retrieve staged actions for human approval or audit inspection."""
    import json
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)
    try:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM staged_actions WHERE business_id = ? AND status = ? ORDER BY id DESC;",
                (biz_id, status),
            )
        else:
            cursor.execute("SELECT * FROM staged_actions WHERE business_id = ? ORDER BY id DESC;", (biz_id,))
        results = []
        for r in cursor.fetchall():
            d = dict(r)
            raw_payload = d.get("payload_json") or d.get("payload") or "{}"
            if isinstance(raw_payload, str):
                try:
                    d["payload"] = json.loads(raw_payload)
                except Exception:
                    d["payload"] = {}
            else:
                d["payload"] = raw_payload
            results.append(d)
        return results
    finally:
        conn.close()

