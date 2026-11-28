"""One-click demo company for visitors and judges.

`ensure_demo_account()` logs into (or creates) a shared demo company, makes sure
the six demo files in `demo_data/` are loaded exactly as shipped, and returns a
normal session. If a previous visitor deleted, unticked or added files, the demo
data is reset first, so every visitor sees the same starting point.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEMO_EMAIL = "demo@raseed-demo.ae"
DEMO_PASSWORD = "RaseedDemo2026"
DEMO_COMPANY = "Al Noor Trading LLC (Demo)"
DEMO_OWNER = "Demo Owner"
DEMO_TRNS = ("100123456789003", "100246813579003", "100975318642003")
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo_data"


def is_demo_session(session: Optional[Dict[str, Any]]) -> bool:
    return bool(session) and str(session.get("email", "")).lower() == DEMO_EMAIL


def _demo_files() -> Dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(DEMO_DIR.glob("*")) if p.is_file()}


def _login_or_create(db_path: Optional[Path] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    from auth.company_auth import authenticate_user, register_company_account

    ok, msg, sess = authenticate_user(DEMO_EMAIL, DEMO_PASSWORD, db_path=db_path)
    if ok and sess:
        return ok, msg, sess
    last = msg
    for trn in DEMO_TRNS:
        ok, msg, _ = register_company_account(
            company_name=DEMO_COMPANY, trn=trn, owner_name=DEMO_OWNER, email=DEMO_EMAIL,
            password=DEMO_PASSWORD, signup_code=None, require_email_verification=False, db_path=db_path,
        )
        if ok:
            break
        last = msg
    ok, msg, sess = authenticate_user(DEMO_EMAIL, DEMO_PASSWORD, db_path=db_path)
    return ok, (msg if ok else last), sess


def _set_profile(business_id: int, db_path: Optional[Path] = None) -> None:
    from database.connection import get_db_connection
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            conn.execute(
                "UPDATE business_profile SET legal_name = ?, vat_registration_date = '2018-01-01', "
                "tax_period_type = 'quarterly', first_tax_period_start = '2018-01-01', "
                "business_activity = COALESCE(business_activity, 'General Trading'), "
                "emirate = COALESCE(emirate, 'Dubai') WHERE business_id = ?;",
                (DEMO_COMPANY, business_id),
            )
    finally:
        conn.close()


def _demo_intact(business_id: int, files: Dict[str, bytes], db_path: Optional[Path] = None) -> bool:
    from ingest.document_manager import list_cloud_documents
    docs = list_cloud_documents(business_id, db_path=db_path)
    if sorted(d["original_filename"] for d in docs) != sorted(files):
        return False
    return all(d["included_in_analysis"] and d["records"] > 0 for d in docs)


def _reset_and_seed(business_id: int, files: Dict[str, bytes], db_path: Optional[Path] = None) -> None:
    from database.connection import get_db_connection
    from ingest.doc_classifier import detect_document_type
    from ingest.document_manager import list_cloud_documents, delete_cloud_documents
    from ingest.document_repair import extract_into_db
    from ingest.file_storage import save_uploaded_file

    existing = [d["id"] for d in list_cloud_documents(business_id, db_path=db_path)]
    if existing:
        delete_cloud_documents(business_id, existing, db_path=db_path)
    conn = get_db_connection(db_path=db_path, readonly=False)
    try:
        with conn:
            # start the demo with no refunds waiting or approved
            conn.execute("DELETE FROM staged_actions WHERE business_id = ?;", (business_id,))
            conn.execute("DELETE FROM credit_ledger WHERE business_id = ?;", (business_id,))
    finally:
        conn.close()

    for name, data in files.items():
        ftype = detect_document_type(data, name)
        doc_id, _dup = save_uploaded_file(file_bytes=data, original_filename=name, document_type=ftype,
                                          business_id=business_id, db_path=db_path)
        if doc_id:
            extract_into_db(business_id, doc_id, ftype, data, name, db_path=db_path)


def ensure_demo_account(db_path: Optional[Path] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Return (ok, message, session) for the demo company, resetting its data if needed."""
    files = _demo_files()
    ok, msg, sess = _login_or_create(db_path=db_path)
    if not ok or not sess:
        return False, f"The demo account could not be opened: {msg}", None
    biz = sess["business_id"]
    _set_profile(biz, db_path=db_path)
    if files and not _demo_intact(biz, files, db_path=db_path):
        _reset_and_seed(biz, files, db_path=db_path)
    from tools.compute_tools import rebuild_credit_ledger
    rebuild_credit_ledger(business_id=biz, db_path=db_path)
    try:
        from agent.recovery_planner import build_or_update_recovery_plan
        build_or_update_recovery_plan(business_id=biz, db_path=db_path)
    except Exception:
        pass
    return True, "Demo ready", sess
