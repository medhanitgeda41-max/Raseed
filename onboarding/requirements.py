"""Statutory onboarding requirements and status checker.

Golden Rule: No calculation or recovery planning may begin until the statutory
minimum documentation threshold is satisfied for the specific business.
"""
from database.request_cache import request_cached
from pathlib import Path
from typing import Optional, Dict, Any, List
from database.connection import get_db_connection, get_or_create_default_business


REQUIRED_ITEMS = [
    "business_profile",
    "purchase_invoices",
    "filed_vat_returns",
]

RECOMMENDED_ITEMS = [
    "sales_invoices_or_register",
    "general_ledger_export",
    "previous_refund_records",
    "official_fta_guides",
]


@request_cached
def onboarding_status(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Check database state and evaluate onboarding completeness for a specific business."""
    conn = get_db_connection(db_path=db_path, readonly=False)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    try:
        cursor = conn.cursor()

        # 1. Check Business Profile
        cursor.execute(
            "SELECT COUNT(*) as cnt, trn, legal_name FROM business_profile WHERE business_id = ? GROUP BY id LIMIT 1;",
            (biz_id,),
        )
        profile_row = cursor.fetchone()
        has_profile = profile_row is not None and profile_row["cnt"] > 0

        # 2. Check Purchase Invoices or Purchase Register
        cursor.execute("SELECT COUNT(*) as cnt FROM invoices WHERE business_id = ? AND direction = 'purchase';", (biz_id,))
        purchase_inv_count = cursor.fetchone()["cnt"]

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM uploaded_documents WHERE business_id = ? AND document_type IN ('invoice', 'register');",
            (biz_id,),
        )
        purchase_doc_count = cursor.fetchone()["cnt"]
        has_purchase_records = (purchase_inv_count > 0) or (purchase_doc_count > 0)

        # 3. Check Filed VAT Returns (Form 201)
        cursor.execute("SELECT COUNT(*) as cnt FROM vat_returns WHERE business_id = ?;", (biz_id,))
        vat_returns_count = cursor.fetchone()["cnt"]

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM uploaded_documents WHERE business_id = ? AND document_type = 'vat_return';",
            (biz_id,),
        )
        vat_returns_doc_count = cursor.fetchone()["cnt"]
        has_vat_returns = (vat_returns_count > 0) or (vat_returns_doc_count > 0)

        # 4. Check Recommended Items
        cursor.execute("SELECT COUNT(*) as cnt FROM invoices WHERE business_id = ? AND direction = 'sales';", (biz_id,))
        sales_count = cursor.fetchone()["cnt"]
        has_sales = sales_count > 0

        cursor.execute("SELECT COUNT(*) as cnt FROM ledger_entries WHERE business_id = ?;", (biz_id,))
        ledger_count = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM uploaded_documents WHERE business_id = ? AND document_type = 'ledger';",
            (biz_id,),
        )
        ledger_doc_count = cursor.fetchone()["cnt"]
        has_ledger = (ledger_count > 0) or (ledger_doc_count > 0)

        cursor.execute("SELECT COUNT(*) as cnt FROM refund_records WHERE business_id = ?;", (biz_id,))
        refund_records_count = cursor.fetchone()["cnt"]
        has_refund_records = refund_records_count > 0

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM uploaded_documents WHERE business_id = ? AND document_type = 'fta_guide';",
            (biz_id,),
        )
        fta_guides_count = cursor.fetchone()["cnt"]
        has_fta_guides = fta_guides_count > 0

        # Compile Lists
        present: List[str] = []
        missing_required: List[str] = []
        missing_recommended: List[str] = []
        invalid: List[str] = []

        if has_profile:
            present.append("business_profile")
        else:
            missing_required.append("business_profile")

        if has_purchase_records:
            present.append("purchase_invoices")
        else:
            missing_required.append("purchase_invoices")

        if has_vat_returns:
            present.append("filed_vat_returns")
        else:
            missing_required.append("filed_vat_returns")

        if has_sales:
            present.append("sales_invoices_or_register")
        else:
            missing_recommended.append("sales_invoices_or_register")

        if has_ledger:
            present.append("general_ledger_export")
        else:
            missing_recommended.append("general_ledger_export")

        if has_refund_records:
            present.append("previous_refund_records")
        else:
            missing_recommended.append("previous_refund_records")

        if has_fta_guides:
            present.append("official_fta_guides")
        else:
            missing_recommended.append("official_fta_guides")

        can_start = len(missing_required) == 0

        total_items = len(REQUIRED_ITEMS) + len(RECOMMENDED_ITEMS)
        progress_percent = round((len(present) / total_items) * 100.0, 1)

        if can_start:
            summary = (
                f"Statutory minimum onboarding criteria satisfied ({len(present)}/{total_items} items provided). "
                "Agent is authorized to begin tax reconciliation."
            )
        else:
            summary = (
                f"Cannot start calculation. Missing {len(missing_required)} required item(s): "
                f"{', '.join(missing_required)}."
            )

        return {
            "business_id": biz_id,
            "can_start": can_start,
            "present": present,
            "missing_required": missing_required,
            "missing_recommended": missing_recommended,
            "invalid": invalid,
            "progress_percent": progress_percent,
            "summary": summary,
            "details": {
                "business_name": profile_row["legal_name"] if profile_row else None,
                "trn": profile_row["trn"] if profile_row else None,
                "purchase_invoices_count": purchase_inv_count,
                "vat_returns_count": vat_returns_count,
                "ledger_entries_count": ledger_count,
            },
        }
    finally:
        conn.close()


def validate_minimum_onboarding(
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Convenience boolean check for starting the agent for a business."""
    status = onboarding_status(business_id=business_id, db_path=db_path)
    return status["can_start"]
