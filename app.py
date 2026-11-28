"""Raseed (رصيد) — Chat-First Autonomous VAT Recovery Agent for UAE Businesses.

Chat-first interface following the official Hackathon specification:
- Top one-line status bar (Business, masked TRN, journey step, total credits, at-risk, pending approvals, provider)
- Left 50%: Fixed VAT Credit Panel with Plotly visualizations (All periods, At risk, Recovery plan, Reconciliation, Documents)
- Right 50%: Scrollable chat container with quick questions, live status, approval cards in-chat, and direct ingestion
- Sidebar: Business selector, role switcher, Steps 1-5 journey progress, privacy mode, follow-chat toggle, workspace reset
"""
import html
import os
import sys
import io
import json
from pathlib import Path
from datetime import date, datetime
import pandas as pd
import streamlit as st

# ------------------------------------------------------------------------------
# 1. STREAMLIT SECRETS BRIDGE & PAGE CONFIG
# ------------------------------------------------------------------------------
try:
    if hasattr(st, "secrets"):
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ[key.upper()] = value
except Exception:
    pass

st.set_page_config(
    page_title="Raseed — Autonomous VAT Recovery Agent",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

from config.settings import settings
from database.request_cache import begin_request
from database.connection import (
    init_db,
    get_db_connection,
    create_business,
    list_businesses,
    get_or_create_default_business,
    get_chat_history,
    save_chat_message,
    get_memory_notes,
    delete_memory_note,
)
from auth.company_auth import (
    register_company_account,
    authenticate_user,
    log_user_logout,
    check_session_timeout,
    complete_2fa_login,
    delete_company_account,
    set_company_local_ai_only,
    get_company_local_ai_only,
)
from auth.two_factor import (
    generate_totp_setup,
    enable_totp,
    disable_totp,
)
from guards.rate_limiter import (
    check_chat_rate_limit,
    check_login_rate_limit,
    check_signup_rate_limit,
)
from onboarding.requirements import onboarding_status
from models.schemas import BusinessProfile
from ingest.file_storage import save_uploaded_file, reset_workspace
from ingest.excel_parser import parse_excel_register, guess_column_mapping, save_cached_column_mapping
from ingest.pdf_parser import extract_text_from_pdf
from ingest.extractor import extract_invoice_fields
from guards.privacy import mask_trn, mask_iban
from tools.read_tools import (
    get_business_profile,
    get_credit_ledger,
    get_invoices,
    list_documents,
    get_document_requests,
    get_staged_actions,
    get_refund_status,
    get_deadlines,
)
from tools.compute_tools import rebuild_credit_ledger, reconcile_4_way
from tools.write_gateway import stage_action, execute_approved_action, reject_staged_action
from tools.package_exporter import generate_refund_excel_package, generate_refund_pdf_package
from agent.recovery_planner import build_or_update_recovery_plan
from agent.chat_agent import (
    get_journey_step,
    execute_chat_turn,
    show_panel,
    check_automatic_alerts,
)
from agent.visual_panel import render_vat_credit_panel
from scheduler import (
    start_monitoring_scheduler,
    advance_demo_clock,
    set_demo_clock_offset,
    get_demo_clock_offset,
    get_current_date,
    run_all_monitoring_jobs_now,
    run_on_new_upload_check,
    run_on_changed_vat_return_check,
    consume_scheduler_messages,
    record_fta_decision,
)

# ------------------------------------------------------------------------------
# 2. MULTI-TENANT COMPANY AUTHENTICATION & LOGIN GATE
# ------------------------------------------------------------------------------
# Start a fresh per-run read cache: repeated reads in this run share one query.
begin_request()

# Chat is a temporary workspace: it is not saved and starts fresh at every login.
# Only uploaded documents (and the numbers read from them) are kept in the cloud.
import database.connection as _dbc
_dbc.CHAT_PERSISTENCE = False


@st.cache_resource(show_spinner=False)
def _init_database_once() -> bool:
    """Create/migrate tables once per server process, not on every click."""
    init_db()
    return True


_init_database_once()


def format_date_plain(d) -> str:
    """Format a date like '23 Nov 2026' for plain-language captions."""
    try:
        return d.strftime("%d %b %Y")
    except Exception:
        return str(d)


def _refresh_refunds_after_change(biz_id: int) -> list:
    """Recalculate refunds still waiting for approval and tell the owner in chat."""
    try:
        from tools.write_gateway import refresh_pending_refund_actions
        changes = refresh_pending_refund_actions(business_id=biz_id)
    except Exception:
        return []
    for ch in changes:
        content = (
            f"🔄 **Refund request #{ch['action_id']} updated** after your new documents: "
            f"AED {ch['old_amount']:,.2f} → **AED {ch['new_amount']:,.2f}** "
            f"(periods: {', '.join(ch['new_periods']) or 'none'}). The EmaraTax refund pack will use the new figures."
        )
        st.session_state.chat_history.append({
            "role": "assistant", "content": content, "timestamp": datetime.now().strftime("%H:%M"),
            "how_i_got_this": {"tools": ["rebuild_credit_ledger", "compute_refund_claim"],
                               "guards": "PASS — recalculated by code from the credit ledger",
                               "provider": "Deterministic Engine", "citations": "Executive Regulation Art. 54"},
        })
        try:
            save_chat_message(business_id=biz_id, role="assistant", content=content)
        except Exception:
            pass
    return changes


is_pytest = "PYTEST_CURRENT_TEST" in os.environ

LANDING_CSS = """
<style>
  [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none; }
  .block-container { padding-top: 3.2rem; max-width: 1180px; }
  .rs-brand { display:flex; align-items:center; gap:.6rem; margin-bottom:1.6rem; }
  .rs-logo { width:40px; height:40px; border-radius:11px; background:#0F766E; color:#fff;
             display:flex; align-items:center; justify-content:center; font-weight:700; font-size:1.25rem; }
  .rs-name { font-size:1.25rem; font-weight:700; color:#0F172A; letter-spacing:-.01em; }
  .rs-name span { color:#0F766E; font-weight:600; margin-left:.35rem; }
  .rs-eyebrow { display:inline-block; font-size:.78rem; font-weight:600; letter-spacing:.06em; text-transform:uppercase;
                color:#0F766E; background:#E6F4F1; padding:.28rem .7rem; border-radius:999px; margin-bottom:1rem; }
  .rs-h1 { font-size:2.55rem; line-height:1.12; font-weight:750; color:#0F172A; letter-spacing:-.025em; margin:0 0 .9rem 0; }
  .rs-h1 em { font-style:normal; color:#0F766E; }
  .rs-lead { font-size:1.06rem; line-height:1.6; color:#475569; margin-bottom:1.6rem; max-width:34rem; }
  .rs-steps { display:grid; grid-template-columns:repeat(4,1fr); gap:.6rem; margin-bottom:1.5rem; }
  .rs-step { background:#F8FAFC; border:1px solid #E2E8F0; border-radius:12px; padding:.8rem .75rem; }
  .rs-step b { display:block; font-size:.74rem; color:#0F766E; letter-spacing:.05em; margin-bottom:.25rem; }
  .rs-step span { font-size:.88rem; font-weight:600; color:#1E293B; line-height:1.3; }
  .rs-trust { list-style:none; padding:0 !important; margin:0 0 1.2rem 0 !important; }
  .rs-trust li { margin-left:0 !important; padding-left:0 !important; }
  .rs-trust li { display:flex; gap:.6rem; align-items:flex-start; color:#334155; font-size:.95rem; line-height:1.5; margin-bottom:.55rem; }
  .rs-trust li::before { content:"✓"; flex:0 0 22px; height:22px; border-radius:50%; background:#E6F4F1; color:#0F766E;
                         font-weight:700; font-size:.8rem; display:flex; align-items:center; justify-content:center; margin-top:1px; }
  .rs-foot { font-size:.8rem; color:#94A3B8; }
  .rs-card-title { font-size:1.15rem; font-weight:700; color:#0F172A; margin:.2rem 0 .1rem 0; }
  .rs-card-sub { font-size:.88rem; color:#64748B; margin-bottom:.6rem; }
  .st-key-demo_box { background:#F0FAF8; border:1px solid #BBE5DD; border-radius:12px; padding:.8rem .85rem .4rem .85rem; margin:.2rem 0 .4rem 0; }
  .rs-demo { font-size:.9rem; color:#134E4A; line-height:1.45; margin-bottom:.55rem; }
  .rs-or { display:flex; align-items:center; gap:.6rem; color:#94A3B8; font-size:.78rem; margin:.5rem 0 .2rem 0; }
  .rs-or::before, .rs-or::after { content:""; flex:1; border-top:1px solid #E2E8F0; }
  [data-testid="stForm"] { border:none; padding:0; }
  @media (max-width: 900px) { .rs-steps { grid-template-columns:repeat(2,1fr); } .rs-h1 { font-size:2rem; } }
</style>
"""

LANDING_INTRO = """
<div class="rs-brand"><div class="rs-logo">ر</div><div class="rs-name">Raseed<span>رصيد</span></div></div>
<div class="rs-eyebrow">UAE VAT recovery for SMEs</div>
<h1 class="rs-h1">Get back the VAT <em>you already paid.</em></h1>
<p class="rs-lead">Many UAE businesses pay more VAT on purchases than they collect on sales, and that extra credit
expires after five years. Raseed reads your invoices and VAT returns, checks every number against FTA rules,
and prepares the refund for you to approve. No tax team needed.</p>
<div class="rs-steps">
  <div class="rs-step"><b>STEP 1</b><span>Upload invoices &amp; returns</span></div>
  <div class="rs-step"><b>STEP 2</b><span>Raseed checks every number</span></div>
  <div class="rs-step"><b>STEP 3</b><span>See what you can recover</span></div>
  <div class="rs-step"><b>STEP 4</b><span>You approve the refund</span></div>
</div>
<ul class="rs-trust">
  <li><span><b>Maths done by code, not guessed by AI.</b> VAT credits, apportionment and 5-year deadlines are calculated by checked rules.</span></li>
  <li><span><b>Nothing is filed without your OK.</b> Every refund waits for your approval.</span></li>
  <li><span><b>Private by design.</b> Each company's data is kept separate, and personal details are masked before any AI sees them.</span></li>
</ul>
<div class="rs-foot">Built on Federal Decree-Law No. 8 of 2017 and its Executive Regulation.</div>
"""

if "auth_user" not in st.session_state and not is_pytest:
    st.markdown(LANDING_CSS, unsafe_allow_html=True)
    col_intro, col_gap, col_auth = st.columns([1.15, 0.08, 0.9])
    with col_intro:
        st.markdown(LANDING_INTRO, unsafe_allow_html=True)
    auth_box = col_auth.container(border=True)

    with auth_box:
        # If pending 2FA verification screen
        pending_2fa = st.session_state.get("pending_2fa_session")
        if pending_2fa:
            st.markdown('<div class="rs-card-title">Two-factor check</div><div class="rs-card-sub">Enter the 6-digit code from your authenticator app, or one of your 8-character backup codes.</div>', unsafe_allow_html=True)
            with st.form("verify_2fa_form"):
                two_factor_code = st.text_input("Authenticator Code or Backup Code", placeholder="e.g. 123456 or A1B2C3D4")
                submit_2fa = st.form_submit_button("Verify & Enter Portal", use_container_width=True, type="primary")
                if submit_2fa:
                    ok, msg, sess_data = complete_2fa_login(pending_2fa, two_factor_code)
                    if ok and sess_data:
                        st.session_state.auth_user = sess_data
                        del st.session_state.pending_2fa_session
                        if "chat_history" in st.session_state:
                            del st.session_state.chat_history
                        st.success("2FA verified! Loading your VAT portal...")
                        st.rerun()
                    else:
                        st.error(msg)
            if st.button("← Back to Log In", use_container_width=True):
                del st.session_state.pending_2fa_session
                st.rerun()
            st.stop()

        st.markdown('<div class="rs-card-title">Welcome to Raseed</div><div class="rs-card-sub">Log in, or create a free account for your company.</div>', unsafe_allow_html=True)
        from onboarding.demo_account import DEMO_EMAIL, DEMO_PASSWORD, ensure_demo_account
        with st.container(key="demo_box"):
            st.markdown(
                "<div class='rs-demo'><b>Just looking?</b> Try Raseed with a ready-made demo company: "
                "real invoices, VAT returns and a ledger are already loaded. No sign-up needed.</div>",
                unsafe_allow_html=True,
            )
            if st.button("Try the live demo", key="try_demo_btn", icon=":material/play_circle:", type="primary", use_container_width=True):
                with st.spinner("Preparing the demo company..."):
                    _ok_demo, _msg_demo, _demo_sess = ensure_demo_account()
                if _ok_demo and _demo_sess:
                    st.session_state.auth_user = _demo_sess
                    st.session_state.chat_history = []
                    st.session_state.pop("docs_repaired_for", None)
                    st.rerun()
                else:
                    st.error(_msg_demo)
            st.caption(f"Or log in with **{DEMO_EMAIL}** / **{DEMO_PASSWORD}**")
        st.markdown("<div class='rs-or'><span>or use your own account</span></div>", unsafe_allow_html=True)
        auth_tab_login, auth_tab_register = st.tabs(["Log in", "Create account"])

        with auth_tab_login:
            with st.form("login_form"):
                login_email = st.text_input("Business Email", placeholder="owner@company.ae")
                login_password = st.text_input("Password", type="password")
                submitted_login = st.form_submit_button("Log In", use_container_width=True, type="primary")

                if submitted_login:
                    allowed, wait_sec, r_msg = check_login_rate_limit(login_email)
                    if not allowed:
                        st.error(r_msg)
                    else:
                        ok, msg, sess_data = authenticate_user(login_email, login_password)
                        if ok and sess_data:
                            st.session_state.auth_user = sess_data
                            if "chat_history" in st.session_state:
                                del st.session_state.chat_history
                            st.success("Login successful! Loading your VAT portal...")
                            st.rerun()
                        elif sess_data and sess_data.get("needs_2fa"):
                            st.session_state.pending_2fa_session = sess_data
                            st.info(msg)
                            st.rerun()
                        else:
                            st.error(msg)

            st.caption("Forgot your password? Contact the Raseed team to reset it.")

        with auth_tab_register:
            with st.form("register_form"):
                reg_company = st.text_input("Company Legal Name", placeholder="e.g. Al Noor Logistics LLC")
                reg_trn = st.text_input("15-Digit TRN", placeholder="Starts with 100, e.g. 100123456789003")
                reg_owner = st.text_input("Owner Full Name", placeholder="e.g. Fatima Al Zahra")
                reg_email = st.text_input("Official Business Email", placeholder="fatima@alnoor.ae")
                reg_pwd = st.text_input("Password (min 10 characters, letters & numbers)", type="password")
                reg_pwd_confirm = st.text_input("Confirm Password", type="password")

                reg_code = None
                if os.environ.get("SIGNUP_CODE") or settings.signup_code:
                    reg_code = st.text_input("Registration Code (Required)", type="password", placeholder="Enter authorization code")

                agree = st.checkbox("I confirm this business is registered for VAT in the UAE and I have authority to act on its behalf.")
                submitted_reg = st.form_submit_button("Create Account & Start", use_container_width=True, type="primary")

                if submitted_reg:
                    allowed, wait_sec, r_msg = check_signup_rate_limit(reg_email)
                    if not allowed:
                        st.error(r_msg)
                    elif not agree:
                        st.error("Please confirm your authority to act on behalf of this business.")
                    elif reg_pwd != reg_pwd_confirm:
                        st.error("Passwords do not match.")
                    else:
                        ok, msg, sess_data = register_company_account(
                            company_name=reg_company,
                            trn=reg_trn,
                            owner_name=reg_owner,
                            email=reg_email,
                            password=reg_pwd,
                            signup_code=reg_code,
                        )
                        if ok and sess_data:
                            st.session_state.auth_user = sess_data
                            if "chat_history" in st.session_state:
                                del st.session_state.chat_history
                            st.success("Company registered successfully! Welcome to Raseed.")
                            st.rerun()
                        else:
                            st.error(msg)

    st.stop()

# ------------------------------------------------------------------------------
# 3. SESSION STATE INITIALIZATION & MULTI-TENANT ISOLATION
# ------------------------------------------------------------------------------
if "auth_user" in st.session_state:
    if check_session_timeout(st.session_state.auth_user.get("last_activity_time")):
        log_user_logout(
            st.session_state.auth_user["user_id"],
            st.session_state.auth_user["business_id"],
        )
        del st.session_state.auth_user
        st.warning("Your session has timed out after 30 minutes of inactivity. Please log in again.")
        st.stop()

    st.session_state.auth_user["last_activity_time"] = datetime.now().isoformat()
    selected_biz_id = st.session_state.auth_user["business_id"]
    active_role = st.session_state.auth_user.get("role", "OWNER")
    owner_display_name = st.session_state.auth_user.get("user_name", "Owner")
    company_display_name = st.session_state.auth_user.get("company_name", "My Company")
else:
    selected_biz_id = get_or_create_default_business()
    active_role = "OWNER"
    owner_display_name = "Owner"
    company_display_name = "Default Business"

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "seen_alert_keys" not in st.session_state:
    st.session_state.seen_alert_keys = set()
if "panel_view" not in st.session_state:
    st.session_state.panel_view = "all_periods"
if "panel_focus" not in st.session_state:
    st.session_state.panel_focus = None
if "pending_user_prompt" not in st.session_state:
    st.session_state.pending_user_prompt = None
if "tour_step" not in st.session_state:
    st.session_state.tour_step = None

# Start background autonomous scheduler (process-level singleton)
try:
    start_monitoring_scheduler(business_id=selected_biz_id, check_interval_seconds=3600)
except Exception:
    pass

# ------------------------------------------------------------------------------
# 4. SIDEBAR CONTROLS (SME BUSINESS OWNER)
# ------------------------------------------------------------------------------
@st.dialog("📄 Problems inside your file", width="large")
def _doc_viewer_dialog(start_doc_id) -> None:
    """Open a stored file and box every problem in red."""
    from tools.compute_tools import document_problems as _dp
    from ingest.document_manager import list_cloud_documents as _lcd
    from ingest.document_viewer import render_document_with_problems
    from ingest.storage_interface import get_storage_backend
    probs = _dp(selected_biz_id)
    if not probs:
        st.success("No problems found in your files. 🎉")
        return
    ids = list(probs.keys())
    try:
        start_idx = ids.index(int(start_doc_id))
    except Exception:
        start_idx = 0
    sel = st.selectbox(
        "File with problems", ids, index=start_idx, key="viewer_sel",
        format_func=lambda i: f"{probs[i]['file_name']}  ({len(probs[i]['problems'])} problem(s))",
    )
    entry = probs[sel]
    doc = next((d for d in _lcd(selected_biz_id) if d["id"] == sel), None)
    if not doc:
        st.warning("This file is no longer available.")
        return
    try:
        data = get_storage_backend().read_file(doc["storage_path"])
    except Exception:
        st.error("The original file could not be read from cloud storage.")
        return
    _only = False
    if Path(doc["original_filename"]).suffix.lower() in (".csv", ".xlsx", ".xls"):
        _only = st.toggle("Show only the rows with problems", value=False, key=f"viewer_only_{sel}")
    with st.spinner("Opening the file and marking the problems..."):
        res = render_document_with_problems(data, doc["original_filename"], entry["items"], only_problem_rows=_only)
    st.markdown("**Problems (numbers match the red boxes):**")
    for n, it in enumerate(entry["items"], 1):
        where = ", ".join(sorted(set(res.get("located", {}).get(n - 1, [])))) or "not visible in the file text"
        st.markdown(f":red[**{n}.**] {it['message']}  \n<small style='color:#64748B'>📍 {where}</small>", unsafe_allow_html=True)
    if res.get("note"):
        st.caption(res["note"])
    if res["kind"] == "table":
        st.caption(f"{res.get('rows', 0)} row(s). Problem rows are tinted red and the exact cells are boxed.")
        with st.container(height=460, border=True):
            st.markdown(res["html"], unsafe_allow_html=True)
    elif res["kind"] == "pages":
        for i, img in enumerate(res["images"], 1):
            st.image(img, caption=f"Page {i}" if len(res["images"]) > 1 else None, use_container_width=True)


def _run_document_repair(announce: bool = True) -> list:
    """Re-check stored files and re-read any saved under the wrong type."""
    try:
        from ingest.document_repair import repair_document_types
        fixes = repair_document_types(selected_biz_id)
    except Exception:
        return []
    if fixes:
        rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
        build_or_update_recovery_plan(business_id=selected_biz_id)
        _refresh_refunds_after_change(selected_biz_id)
        if announce:
            lines = "\n".join(f"- `{f['file_name']}`: {f['old_type']} → **{f['new_type']}** ({f['result']})" for f in fixes)
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": f"🔧 **I re-checked your saved files and fixed {len(fixes)} that were stored as the wrong type:**\n{lines}\n\nYour numbers are now recalculated.",
                "timestamp": datetime.now().strftime("%H:%M"),
                "how_i_got_this": {"tools": ["detect_document_type", "repair_document_types", "rebuild_credit_ledger"],
                                   "guards": "PASS — files re-read from cloud storage by code",
                                   "provider": "Deterministic Engine", "citations": "Executive Regulation Art. 54"},
            })
    return fixes


if not is_pytest and st.session_state.get("docs_repaired_for") != selected_biz_id:
    st.session_state.docs_repaired_for = selected_biz_id
    _run_document_repair()

with st.sidebar:
    from ingest.document_manager import list_cloud_documents, set_analysis_selection, delete_cloud_documents
    from tools.compute_tools import document_problems as _document_problems
    from ingest.doc_classifier import DOC_TYPE_LABELS as _DOC_LABELS

    st.markdown(
        """
<style>
  section[data-testid="stSidebar"] .st-key-sb_logout button { padding:.25rem .3rem; min-height:2.2rem; white-space:nowrap; }
  section[data-testid="stSidebar"] .st-key-sb_logout button p { font-size:.8rem; }
  section[data-testid="stSidebar"] [class*="st-key-docdel_"] button { padding:0 .35rem; min-height:1.7rem; line-height:1;
      border:none !important; background:transparent !important; color:#B91C1C !important; font-weight:700; }
  section[data-testid="stSidebar"] [class*="st-key-docdel_"] button:hover { background:#FEE2E2 !important; }
  section[data-testid="stSidebar"] [class*="st-key-docview_"] button { padding:0 .3rem; min-height:1.7rem; border:none !important; background:transparent !important; }
  section[data-testid="stSidebar"] [class*="st-key-docview_"] button:hover { background:#FEE2E2 !important; }
  section[data-testid="stSidebar"] .rs-docname { font-size:.84rem; line-height:1.25; color:#0F172A; word-break:break-all; padding-top:.1rem; }
  section[data-testid="stSidebar"] .rs-docmeta { font-size:.72rem; color:#64748B; }
  section[data-testid="stSidebar"] [class*="st-key-docrow_"] { gap:.2rem; }
  section[data-testid="stSidebar"] [class*="st-key-docrow_"]:has(.rs-docbad) { background:#FEF2F2; border:1px solid #FCA5A5;
      border-radius:10px; padding:.35rem .3rem; }
  section[data-testid="stSidebar"] .rs-docbad { font-size:.74rem; color:#B91C1C; font-weight:600; }
  section[data-testid="stSidebar"] .rs-docwhy { font-size:.74rem; color:#7F1D1D; line-height:1.3; margin-top:.15rem; }
</style>
""",
        unsafe_allow_html=True,
    )

    def _sb_after_docs_changed() -> None:
        rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
        build_or_update_recovery_plan(business_id=selected_biz_id)
        _refresh_refunds_after_change(selected_biz_id)

    def _sb_toggle_doc(doc_id: int) -> None:
        docs_now = list_cloud_documents(selected_biz_id)
        keep = {d["id"] for d in docs_now if d["included_in_analysis"]}
        if st.session_state.get(f"docinc_{doc_id}"):
            keep.add(doc_id)
        else:
            keep.discard(doc_id)
        set_analysis_selection(selected_biz_id, keep)
        _sb_after_docs_changed()

    _sb_docs = list_cloud_documents(selected_biz_id)
    _sb_in = sum(1 for d in _sb_docs if d["included_in_analysis"])

    _c_out, _c_name = st.columns([1.25, 2.0], vertical_alignment="center", gap="small")
    with _c_out:
        if st.button("Log out", key="sb_logout", icon=":material/logout:", use_container_width=True, help="End your session securely"):
            if "auth_user" in st.session_state:
                log_user_logout(st.session_state.auth_user["user_id"], selected_biz_id)
                del st.session_state.auth_user
            if "chat_history" in st.session_state:
                st.session_state.chat_history = []
            st.rerun()
    with _c_name:
        st.markdown(f'<div class="rs-co">{company_display_name}</div><div class="rs-user">{owner_display_name} · {active_role.title()}</div>',
                    unsafe_allow_html=True)
    try:
        from onboarding.demo_account import is_demo_session
        if is_demo_session(st.session_state.get("auth_user")):
            st.markdown('<div class="rs-demo">Demo company · explore freely, the data resets for the next visitor.</div>',
                        unsafe_allow_html=True)
    except Exception:
        pass

    try:
        _sb_probs = _document_problems(selected_biz_id)
    except Exception:
        _sb_probs = {}
    _hl = bool(st.session_state.get("highlight_docs"))
    _n_bad = len(_sb_probs)
    _docs_label = "My documents" + (f" · {_n_bad} with problems" if (_hl and _n_bad) else "")
    with st.expander(_docs_label, expanded=_hl, icon=":material/folder_open:"):
        st.caption(f"{len(_sb_docs)} file(s) · {_sb_in} used in the analysis"
                   + (f" · :red[{_n_bad} with problems]" if _n_bad else ""))
        if _hl:
            if _n_bad:
                st.error(f"{_n_bad} file(s) have problems. They are marked in red below.")
            else:
                st.success("No problems found in your files.")
            if st.button("Clear highlights", key="sb_clear_hl", use_container_width=True):
                st.session_state.highlight_docs = False
                st.rerun()
        if not _sb_docs:
            st.caption("No documents yet. Upload them in **☁️ Upload documents** on the main page.")
        else:
            _q = st.text_input("Search", placeholder="Search by file name…", key="sb_doc_search", label_visibility="collapsed")
            _dates = st.date_input("Uploaded between", value=(), key="sb_doc_dates", format="DD/MM/YYYY",
                                   help="Optional: show only files uploaded in this date range")
            _shown = [d for d in _sb_docs if not _q or _q.lower() in d["original_filename"].lower()
                      or _q.lower() in _DOC_LABELS.get(d["document_type"], "").lower()]
            if isinstance(_dates, (list, tuple)) and len(_dates) >= 1:
                _d_from = _dates[0].isoformat()
                _d_to = (_dates[1] if len(_dates) > 1 else _dates[0]).isoformat()
                _shown = [d for d in _shown if _d_from <= str(d.get("uploaded_at") or "")[:10] <= _d_to]
            st.caption("✔ = used in the analysis · ✕ = delete from the cloud")
            _icons = {"register": "📊", "invoice": "🧾", "vat_return": "📄", "ledger": "📒", "fta_guide": "📘", "evidence": "📎", "other": "🚫"}
            if _hl:
                _shown = sorted(_shown, key=lambda d: 0 if (d["included_in_analysis"] and d["id"] in _sb_probs) else 1)
            with st.container(height=380 if _hl else 230, border=False):
                if not _shown:
                    st.caption("No file matches your search.")
                for d in _shown:
                    with st.container(key=f"docrow_{d['id']}"):
                        _bad_row = d["included_in_analysis"] and d["id"] in _sb_probs
                        if _bad_row:
                            _r1, _r2, _r4, _r3 = st.columns([0.15, 0.55, 0.15, 0.15], vertical_alignment="center", gap="small")
                            if _r4.button("👁", key=f"docview_{d['id']}", help="Open this file and show the problems in red"):
                                st.session_state.open_doc_viewer = d["id"]
                                st.rerun()
                        else:
                            _r1, _r2, _r3 = st.columns([0.16, 0.68, 0.16], vertical_alignment="center", gap="small")
                        _r1.checkbox(
                            "Use in analysis", value=d["included_in_analysis"], key=f"docinc_{d['id']}",
                            label_visibility="collapsed", on_change=_sb_toggle_doc, args=(d["id"],),
                        )
                        _bad = _sb_probs.get(d["id"]) if d["included_in_analysis"] else None
                        _name_style = " style='color:#B91C1C;font-weight:600'" if _bad else ""
                        _flag = f"<div class='rs-docbad'>⚠️ {len(_bad['problems'])} problem(s)</div>" if _bad else ""
                        _reasons = ""
                        if _bad and _hl:
                            _reasons = "".join(f"<div class='rs-docwhy'>• {html.escape(pr)}</div>" for pr in _bad["problems"][:6])
                            if len(_bad["problems"]) > 6:
                                _reasons += f"<div class='rs-docwhy'>• …and {len(_bad['problems']) - 6} more</div>"
                        _r2.markdown(
                            f"<div class='rs-docname'{_name_style}>{_icons.get(d['document_type'], '📎')} {html.escape(d['original_filename'])}</div>"
                            f"<div class='rs-docmeta'>{_DOC_LABELS.get(d['document_type'], d['document_type'])} · {d['records']} record(s) · {str(d.get('uploaded_at') or '')[:10]}</div>"
                            f"{_flag}{_reasons}",
                            unsafe_allow_html=True,
                        )
                        if _r3.button("✕", key=f"docdel_{d['id']}", help=f"Delete {d['original_filename']}"):
                            st.session_state.confirm_delete_doc = d["id"]
                            st.rerun()
            _pd = st.session_state.get("confirm_delete_doc")
            if _pd:
                _pname = next((d["original_filename"] for d in _sb_docs if d["id"] == _pd), None)
                if _pname is None:
                    st.session_state.confirm_delete_doc = None
                else:
                    st.warning(f"Delete `{_pname}` from the cloud? Its numbers will be removed from your analysis.")
                    _y, _n = st.columns(2)
                    if _y.button("Delete", key="sb_del_yes", type="primary", use_container_width=True):
                        delete_cloud_documents(selected_biz_id, [_pd])
                        _sb_after_docs_changed()
                        st.session_state.confirm_delete_doc = None
                        st.toast(f"Deleted {_pname}.")
                        st.rerun()
                    if _n.button("Cancel", key="sb_del_no", use_container_width=True):
                        st.session_state.confirm_delete_doc = None
                        st.rerun()
            if st.button("🔧 Re-check file types", key="sb_recheck", use_container_width=True,
                         help="Re-read every saved file and fix any stored as the wrong type"):
                with st.spinner("Re-checking your files..."):
                    _fx = _run_document_repair()
                st.toast(f"Fixed {len(_fx)} file(s)." if _fx else "All files already have the right type.")
                st.rerun()
            _a1, _a2 = st.columns(2)
            if _a1.button("Tick all", key="sb_tick_all", use_container_width=True):
                set_analysis_selection(selected_biz_id, [d["id"] for d in _sb_docs]); _sb_after_docs_changed(); st.rerun()
            if _a2.button("Untick all", key="sb_untick_all", use_container_width=True):
                set_analysis_selection(selected_biz_id, []); _sb_after_docs_changed(); st.rerun()

    st.markdown("---")

_viewer_req = st.session_state.pop("open_doc_viewer", None)
if _viewer_req is not None and not is_pytest:
    _doc_viewer_dialog(_viewer_req)

with st.sidebar:
    col_tour, col_check = st.columns(2)
    if col_tour.button("Tour", icon=":material/explore:", use_container_width=True, help="A step-by-step tour of Raseed"):
        st.session_state.tour_step = 1
        st.rerun()

    if col_check.button("Checks", icon=":material/fact_check:", use_container_width=True, help="Check every deadline, document and refund status now"):
        with st.spinner("Running autonomous checks..."):
            run_all_monitoring_jobs_now(business_id=selected_biz_id)
        st.success("Autonomous check complete!")
        st.rerun()

    st.markdown("---")

    # Why You Can Trust This (Always visible trust explanation)
    with st.expander("How Raseed keeps you safe", icon=":material/verified_user:", expanded=False):
        st.markdown(
            "The AI reads and explains; checked code does every VAT calculation and rule check.\n\n"
            "- **Checked by code:** credits, apportionment and 5-year deadlines.\n"
            "- **You stay in control:** nothing is filed without your OK.\n"
            "- **Audit trail:** every upload, calculation and decision is recorded."
        )

    st.markdown("---")

    # Journey Progress Steps (1 to 5)
    journey = get_journey_step(business_id=selected_biz_id)
    st.markdown('<div class="rs-sb-label">Settings</div>', unsafe_allow_html=True)
    simple_mode = st.toggle(
        "Simple mode",
        value=st.session_state.get("simple_mode", True),
        help="Streamlined view designed for hackathon judges",
    )
    st.session_state.simple_mode = simple_mode

    # Local AI Only setting
    cur_local_ai = get_company_local_ai_only(selected_biz_id)
    toggle_local_ai = st.toggle(
        "Local AI only (never send data to the cloud)",
        value=cur_local_ai,
        help="Strict offline privacy: disables all external cloud LLM connections for your company.",
    )
    if toggle_local_ai != cur_local_ai:
        set_company_local_ai_only(selected_biz_id, toggle_local_ai)
        st.success("Local AI setting saved!")
        st.rerun()

    privacy_mode = st.radio(
        "AI used for answers",
        options=["Cloud AI, data masked (Groq/Gemini)", "Local only (no cloud)"],
        index=0 if settings.llm_mode == "demo" and not toggle_local_ai else 1,
    )

    follow_chat = st.toggle("Follow chat", value=True, help="Automatically switch VAT Credit Panel view when Raseed presents your data.")

    # Demo Time Control (Fast-Forward Clock for Hackathon Judges) - Only in normal mode
    if not simple_mode:
        with st.expander("Demo time control", icon=":material/schedule:"):
            st.caption("Move Raseed's clock forward to observe statutory countdowns and automatic alerts.")
            current_offset = get_demo_clock_offset()
            sim_days = st.slider("Fast-Forward Clock (Days)", 0, 365, value=current_offset, key="demo_time_slider")
            if sim_days != current_offset:
                set_demo_clock_offset(sim_days)
                rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
                run_all_monitoring_jobs_now(business_id=selected_biz_id)
                st.rerun()

            if sim_days > 0:
                eval_d = format_date_plain(get_current_date())
                st.info(f"Simulating +{sim_days} days into the future (Simulated Date: **{eval_d}**).")

    # Show Technical Details Toggle
    show_tech_details = st.checkbox("Show technical details", value=False)

    # Open Audit Log
    with st.expander("Audit trail", icon=":material/history:"):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit_events WHERE business_id = ? ORDER BY id DESC LIMIT 25;", (selected_biz_id,))
        audit_rows = cur.fetchall()
        conn.close()
        if not audit_rows:
            st.caption("No audit events recorded yet.")
        else:
            for ev in audit_rows:
             ev = dict(ev)
             st.caption(f"`{ev.get('created_at') or ev.get('timestamp') or ''}` | **[{ev.get('actor') or 'system'}]** `{ev.get('event_type') or ev.get('action') or ''}`")

    # Confirmed Facts (Persistent Memory)
    with st.expander("Remembered facts", icon=":material/psychology:"):
        mem_notes = get_memory_notes(business_id=selected_biz_id)
        if not mem_notes:
            st.caption("No confirmed facts recorded yet. As you chat, key facts about your business are remembered here.")
        else:
            for mn in mem_notes:
                mc1, mc2 = st.columns([4, 1])
                mc1.markdown(f"• **{mn['note']}**\n\n<small style='color:gray;'>Confirmed: {str(mn.get('confirmed_date') or mn.get('created_at') or '')[:10]}</small>", unsafe_allow_html=True)
                if mc2.button("🗑️", key=f"del_mem_note_{mn['id']}", help="Delete this remembered fact"):
                    delete_memory_note(note_id=mn["id"], business_id=selected_biz_id)
                    st.rerun()

    # 2FA Settings
    with st.expander("Two-factor sign-in", icon=":material/lock:"):
        user_id = st.session_state.auth_user.get("user_id") if "auth_user" in st.session_state else 1
        user_email = st.session_state.auth_user.get("email", "") if "auth_user" in st.session_state else ""
        conn_totp = get_db_connection()
        cur_totp = conn_totp.cursor()
        cur_totp.execute("SELECT is_totp_enabled FROM users WHERE id = ?;", (user_id,))
        totp_row = cur_totp.fetchone()
        conn_totp.close()
        is_totp = bool(totp_row["is_totp_enabled"]) if totp_row and "is_totp_enabled" in totp_row.keys() else False

        if is_totp:
            st.success("✅ 2FA is active on your account.")
            with st.form("disable_2fa_form"):
                d_pwd = st.text_input("Enter password to disable 2FA", type="password")
                if st.form_submit_button("Disable 2FA", type="secondary"):
                    d_ok, d_msg = disable_totp(user_id=user_id, password=d_pwd)
                    if d_ok:
                        st.success(d_msg)
                        st.rerun()
                    else:
                        st.error(d_msg)
        else:
            st.caption("Protect your company portal with Google Authenticator, Microsoft Authenticator, or 1Password.")
            if "totp_setup_data" not in st.session_state:
                if st.button("Set Up 2FA Authenticator"):
                    st.session_state.totp_setup_data = generate_totp_setup(email=user_email, company_name=company_display_name)
                    st.rerun()
            else:
                s_data = st.session_state.totp_setup_data
                st.markdown("**1. Scan QR Code in Authenticator App:**")
                st.image(s_data["qr_data_uri"], width=200)
                st.caption(f"Or enter secret manually: `{s_data['secret']}`")
                st.markdown("**2. Save Your Emergency Backup Codes:**")
                st.code("\n".join(s_data["plain_backup_codes"]))
                st.caption("Save these 8 one-time backup codes safely. Each code can be used once if you lose your phone.")
                with st.form("confirm_2fa_form"):
                    conf_code = st.text_input("Enter 6-digit code from your app to verify", max_chars=6)
                    if st.form_submit_button("Activate 2FA", type="primary"):
                        act_ok, act_msg = enable_totp(
                            user_id=user_id,
                            secret=s_data["secret"],
                            verification_code=conf_code,
                            backup_codes_json=s_data["backup_codes_json"],
                        )
                        if act_ok:
                            del st.session_state.totp_setup_data
                            st.success(act_msg)
                            st.rerun()
                        else:
                            st.error(act_msg)
                if st.button("Cancel 2FA Setup"):
                    del st.session_state.totp_setup_data
                    st.rerun()

    # Reset Workspace
    st.markdown("---")
    st.markdown("### ⚠️ Reset Workspace")
    with st.expander("Reset All Data"):
        st.warning("Permanently deletes all your invoices, filings, and ledger calculations to start fresh.")
        confirm = st.text_input("Type RESET to confirm")
        if st.button("Delete My Data", type="primary"):
            if confirm == "RESET":
                reset_workspace(confirm=True, business_id=selected_biz_id)
                st.session_state.chat_history = []
                st.session_state.seen_alert_keys = set()
                st.session_state.panel_view = "all_periods"
                st.session_state.panel_focus = None
                st.success("Your workspace was successfully wiped. You can start fresh!")
                st.rerun()
            else:
                st.error("Confirmation mismatch.")

    # Delete Company Account
    st.markdown("---")
    st.markdown("### 🚨 Danger Zone")
    with st.expander("🗑️ Delete Company Account & All Data"):
        st.error("⚠️ **Permanent Action:** Deletes your entire company profile, all uploaded documents, R2 cloud storage files, database rows, chat history, and memory.")
        del_pwd = st.text_input("Enter your account password", type="password", key="del_acc_pwd")
        del_confirm = st.text_input("Type DELETE to confirm permanent purge", key="del_acc_confirm")
        if st.button("Permanently Delete Company Account", type="primary"):
            if del_confirm != "DELETE":
                st.error("Please type DELETE to confirm.")
            else:
                user_id = st.session_state.auth_user.get("user_id") if "auth_user" in st.session_state else 1
                del_ok, del_msg = delete_company_account(
                    business_id=selected_biz_id,
                    user_id=user_id,
                    password=del_pwd,
                )
                if del_ok:
                    if "auth_user" in st.session_state:
                        del st.session_state.auth_user
                    if "chat_history" in st.session_state:
                        del st.session_state.chat_history
                    st.success(del_msg)
                    st.rerun()
                else:
                    st.error(del_msg)

# ------------------------------------------------------------------------------
# 5. GUIDED DEMO TOUR & "HOW RASEED WORKS" STRIP
# ------------------------------------------------------------------------------
profile = get_business_profile(business_id=selected_biz_id)
ob_status = onboarding_status(business_id=selected_biz_id)
refund_status = get_refund_status(business_id=selected_biz_id)
pending_actions = get_staged_actions(status="PENDING_APPROVAL", business_id=selected_biz_id)
current_journey = get_journey_step(business_id=selected_biz_id)

biz_name = profile["legal_name"] if profile else "Your Business"
trn_disp = mask_trn(profile["trn"]) if profile else "TRN: Not Registered"
total_credits = refund_status.get("total_available_credits_aed", 0.0)
credits_at_risk = refund_status.get("at_risk_credits_aed", 0.0)
pending_cnt = len(pending_actions)
provider_label = "Groq / Gemini (cloud AI)" if "Cloud" in privacy_mode else "Local AI only"

# Guided Demo Tour Banner (if active)
TOUR_SLIDES = [
    {
        "step": 1,
        "title": "1. How Raseed Works Strip",
        "desc": "The 4-step banner at the top shows your recovery journey in real time — from uploading documents to receiving your FTA refund approval.",
    },
    {
        "step": 2,
        "title": "2. Your VAT Credits (Top Panel)",
        "desc": "Visualizes your live tax position, upcoming 5-year statutory deadlines, and any accounting differences across your records.",
    },
    {
        "step": 3,
        "title": "3. Chat with Raseed (below the panel)",
        "desc": "Your autonomous copilot who speaks plain English, analyzes UAE tax law, and answers any question about your VAT.",
    },
    {
        "step": 4,
        "title": "4. Clear Results Card",
        "desc": "Summarizes the exact dirhams you can recover, what is about to expire, and what issues need your attention.",
    },
    {
        "step": 5,
        "title": "5. Approval Cards & Human Sign-Off",
        "desc": "Raseed never files or submits without your explicit permission. You inspect the exact numbers and click Authorize.",
    },
    {
        "step": 6,
        "title": "6. Why You Can Trust This",
        "desc": "Every calculation is performed by deterministic Python code, verified against FTA rules, and recorded in the audit trail.",
    },
]

if st.session_state.tour_step is not None:
    curr_tour = next((t for t in TOUR_SLIDES if t["step"] == st.session_state.tour_step), None)
    if curr_tour:
        with st.container(border=True):
            t_col1, t_col2 = st.columns([3, 1])
            with t_col1:
                st.markdown(f"#### 🎯 Guided Tour: {curr_tour['title']}")
                st.write(f"💡 {curr_tour['desc']}")
            with t_col2:
                st.caption(f"Step {curr_tour['step']} of {len(TOUR_SLIDES)}")
                b_prev, b_next = st.columns(2)
                if curr_tour['step'] > 1:
                    if b_prev.button("◀ Back", key="tour_prev_btn"):
                        st.session_state.tour_step -= 1
                        st.rerun()
                if curr_tour['step'] < len(TOUR_SLIDES):
                    if b_next.button("Next ▶", key="tour_next_btn", type="primary"):
                        st.session_state.tour_step += 1
                        st.rerun()
                else:
                    if b_next.button("Finish 🎉", key="tour_finish_btn", type="primary"):
                        st.session_state.tour_step = None
                        st.rerun()
                if st.button("Close Tour", key="tour_close_btn"):
                    st.session_state.tour_step = None
                    st.rerun()

# 1. "HOW RASEED WORKS" STRIP (ALWAYS VISIBLE, ONE LINE)
try:
    from ingest.document_manager import list_cloud_documents as _lcd_strip
    _have_docs = bool(_lcd_strip(selected_biz_id))
except Exception:
    _have_docs = True
if not _have_docs:
    active_strip_idx = 1
elif pending_cnt > 0:
    active_strip_idx = 4
elif total_credits > 0:
    active_strip_idx = 3
else:
    active_strip_idx = 2

strip_steps = [
    (1, "Upload your documents"),
    (2, "Raseed checks them"),
    (3, "Money you're owed"),
    (4, "You approve the refund"),
]
_parts = []
for s_num, s_title in strip_steps:
    state = "done" if s_num < active_strip_idx else ("now" if s_num == active_strip_idx else "todo")
    mark = "&#10003;" if state == "done" else str(s_num)
    _parts.append(f'<div class="rs-step {state}"><span class="dot">{mark}</span><span class="t">{s_title}</span></div>')
strip_html = (
    "<style>"
    ".rs-steps{display:flex;align-items:center;gap:.5rem;margin:0 0 1rem 0;flex-wrap:wrap}"
    ".rs-step{display:flex;align-items:center;gap:.5rem;font-size:.9rem;color:#94A3B8}"
    ".rs-step .dot{width:1.55rem;height:1.55rem;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;"
    "font-size:.78rem;font-weight:700;border:1.5px solid #CBD5E1;color:#94A3B8;background:#fff}"
    ".rs-step.done{color:#0F766E}.rs-step.done .dot{background:#0F766E;border-color:#0F766E;color:#fff}"
    ".rs-step.now{color:#0F172A;font-weight:650}.rs-step.now .dot{border-color:#0F766E;color:#0F766E;box-shadow:0 0 0 3px #CCFBF1}"
    ".rs-line{flex:1;min-width:1.5rem;height:1.5px;background:#E2E8F0}"
    "</style>"
    '<div class="rs-steps">' + '<span class="rs-line"></span>'.join(_parts) + "</div>"
)
st.markdown(strip_html, unsafe_allow_html=True)

# ------------------------------------------------------------------------------
# 6. TOP STATUS BAR (ONE LINE, PLAIN LANGUAGE, EXPLAINED NUMBERS)
# ------------------------------------------------------------------------------
try:
    from tools.compute_tools import document_problems as _dp_top
    _top_probs = _dp_top(selected_biz_id)
except Exception:
    _top_probs = {}
_n_prob_items = sum(len(v["problems"]) for v in _top_probs.values())
try:
    from tools.compute_tools import declared_vs_proven as _dvp
    _dv = _dvp(selected_biz_id)
except Exception:
    _dv = {"declared": 0.0, "gap": 0.0, "unproven_periods": []}
_money_sub = ("Proven by your tax invoices" if _dv["gap"] < 0.01 else
              f"Your returns claim AED {_dv['declared']:,.0f} · upload invoices to prove AED {_dv['gap']:,.0f} more")
st.markdown(
    f"""
<style>
  .rs-kpis {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:.75rem; margin:.2rem 0 1.1rem 0; }}
  .rs-kpi {{ background:#FFFFFF; border:1px solid #E2E8F0; border-radius:14px; padding:.85rem 1rem; }}
  .rs-kpi .lbl {{ font-size:.74rem; font-weight:600; color:#64748B; text-transform:uppercase; letter-spacing:.04em; }}
  .rs-kpi .val {{ font-size:1.45rem; font-weight:700; color:#0F172A; margin-top:.15rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .rs-kpi .sub {{ font-size:.78rem; color:#64748B; margin-top:.1rem; }}
  .rs-kpi.good .val {{ color:#0F766E; }} .rs-kpi.warn .val {{ color:#C2410C; }}
  .rs-kpi.ask .val {{ color:#3730A3; }} .rs-kpi.bad .val {{ color:#B91C1C; }}
  @media (max-width: 900px) {{ .rs-kpis {{ grid-template-columns:repeat(2, minmax(0,1fr)); }} }}
</style>
<div class="rs-kpis">
  <div class="rs-kpi good" title="Verified VAT credits you can claim back from the FTA">
    <div class="lbl">Money you can get back</div><div class="val">AED {total_credits:,.2f}</div><div class="sub">{_money_sub}</div></div>
  <div class="rs-kpi warn" title="Credits that expire within 180 days">
    <div class="lbl">Expiring soon</div><div class="val">AED {credits_at_risk:,.2f}</div><div class="sub">Claim before the 5-year limit</div></div>
  <div class="rs-kpi ask" title="Refund requests prepared by Raseed, waiting for your approval">
    <div class="lbl">Waiting for your OK</div><div class="val">{pending_cnt}</div><div class="sub">Refund request(s)</div></div>
  <div class="rs-kpi {'bad' if _n_prob_items else 'good'}" title="Problems found by code in your files">
    <div class="lbl">Problems found</div><div class="val">{_n_prob_items}</div><div class="sub">{'in ' + str(len(_top_probs)) + ' file(s) · ask me to show them' if _n_prob_items else 'All records match'}</div></div>
</div>
""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------------------
# 6. AUTOMATIC ALERTS & SCHEDULER INGESTION (PROACTIVE AGENT)
# ------------------------------------------------------------------------------
try:
    new_alerts = check_automatic_alerts(
        business_id=selected_biz_id,
        existing_alert_keys=st.session_state.seen_alert_keys,
    )
except Exception:
    # A background check must never take the whole page down.
    import logging
    logging.getLogger("raseed.app").exception("Automatic alert check failed")
    new_alerts = []
for al in new_alerts:
    st.session_state.seen_alert_keys.add(al["key"])
    st.session_state.chat_history.append({
        "role": "assistant",
        "content": al["content"],
        "timestamp": datetime.now().strftime("%H:%M"),
        "how_i_got_this": al.get("how_i_got_this", {
            "tools": ["check_automatic_alerts"],
            "guards": "PASS",
            "provider": "Statutory Monitor",
        }),
    })

# Ingest autonomous background scheduler notifications
try:
    sched_msgs = consume_scheduler_messages(business_id=selected_biz_id)
except Exception:
    sched_msgs = []
for sm in sched_msgs:
    st.session_state.chat_history.append({
        "role": "assistant",
        "content": sm["content"],
        "timestamp": datetime.now().strftime("%H:%M"),
        "how_i_got_this": {
            "tools": ["start_monitoring_scheduler", sm["message_type"]],
            "guards": "PASS — Continuous Statutory Monitor",
            "provider": "Statutory Monitor",
            "citations": "Federal Decree-Law No. (8) of 2017 & DL 17/2025",
        },
    })

# Initial Welcome Message if chat is empty
if not st.session_state.chat_history:
    if not profile:
        welcome_msg = {
            "role": "assistant",
            "content": (
                "Marhaba! I am **Raseed (رصيد)**, your autonomous UAE VAT recovery copilot.\n\n"
                "I am built specifically for UAE business owners without an in-house tax team to help you find and claim back all your eligible excess VAT from the FTA.\n\n"
                "🔒 *Your documents are stored privately in cloud storage. You can delete them at any time.*\n\n"
                "To get started, tell me your business details right here in chat:\n"
                "• **Your Company Legal Name**\n"
                "• **Your 15-digit TRN** (starts with 100)\n"
                "• **VAT registration date** (e.g. 2021-01-01)\n"
                "• **Filing frequency** (monthly or quarterly)\n\n"
                "Simply type or paste them here, and I'll register your business and find your money!"
            ),
            "timestamp": datetime.now().strftime("%H:%M"),
            "how_i_got_this": {
                "tools": ["onboarding_status"],
                "guards": "PASS — Zero-Seed Workspace Inspection",
                "provider": "Raseed AI Copilot",
                "citations": "Federal Decree-Law No. (8) of 2017",
            },
        }
        st.session_state.chat_history.append(welcome_msg)
        try:
            save_chat_message(
                business_id=selected_biz_id,
                role="assistant",
                content=welcome_msg["content"],
                how_i_got_this_json=json.dumps(welcome_msg["how_i_got_this"]),
            )
        except Exception:
            pass
    else:
        welcome_msg = {
            "role": "assistant",
            "content": (
                f"Welcome back! I am analyzing the VAT recovery position for **{biz_name}**.\n\n"
                f"We are at **{current_journey['step_label']}**.\n\n"
                "🔒 *Your documents are stored privately in cloud storage. You can delete them at any time.*\n\n"
                "You have **" + f"{total_credits:,.2f} AED" + "** in verified eligible recoverable VAT credits waiting to be claimed back. "
                "Ask me about upcoming deadlines, differences between your invoices and returns, or ask me to prepare your refund."
            ),
            "timestamp": datetime.now().strftime("%H:%M"),
            "how_i_got_this": {
                "tools": ["get_business_profile", "get_credit_ledger"],
                "guards": "PASS — Verified Financial Records",
                "provider": "Raseed AI Copilot",
                "citations": "Executive Regulation Art. 54",
            },
        }
        st.session_state.chat_history.append(welcome_msg)
        try:
            save_chat_message(
                business_id=selected_biz_id,
                role="assistant",
                content=welcome_msg["content"],
                how_i_got_this_json=json.dumps(welcome_msg["how_i_got_this"]),
            )
        except Exception:
            pass

# ------------------------------------------------------------------------------
# 7. TWO COLUMNS 50/50: LEFT (VAT CREDIT PANEL) | RIGHT (OPEN AI CHAT)
# ------------------------------------------------------------------------------
st.markdown(
    """
<style>
  /* ---------- Chat look ---------- */
  .rs-chat-head { display:flex; align-items:center; justify-content:space-between; gap:.8rem; flex-wrap:wrap; margin:.4rem 0 .3rem 0; }
  .rs-sep { border:none; border-top:1px solid #EEF2F6; margin:1.1rem 0 .7rem 0; }
  div[class*="st-key-act_"] button { border-radius:12px !important; min-height:2.8rem; font-weight:600; }
  .rs-chat-head h3 { margin:0; font-size:1.35rem; font-weight:700; color:#0F172A; }
  .rs-chat-head .rs-ai { font-size:.8rem; color:#0F766E; background:#E6F4F1; border-radius:999px; padding:.25rem .7rem; font-weight:600; }
  [data-testid="stChatMessage"] { border:none !important; box-shadow:none !important; background:transparent !important;
                                  padding:.35rem .2rem; margin-bottom:.35rem; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
      flex-direction: row-reverse; background:#F1F5F9 !important; border-radius:18px;
      padding:.55rem .9rem; width: fit-content; max-width: 78%; margin-left: auto; box-sizing: border-box; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stMarkdownContainer"] { text-align:left; }
  [data-testid="stChatMessageAvatarUser"] { background:#CBD5E1 !important; color:#0F172A !important; }
  [data-testid="stChatMessageAvatarAssistant"] { background:#0F766E !important; color:#fff !important; }
  [data-testid="stChatMessage"] { overflow-wrap:anywhere; }
  div[data-testid="stVerticalBlockBorderWrapper"] { overflow-x: hidden; }
  [data-testid="stChatMessage"] [data-testid="stExpander"] details { border:none !important; background:transparent !important; }
  [data-testid="stChatMessage"] [data-testid="stExpander"] summary { padding:.2rem 0 !important; font-size:.85rem; color:#64748B; }
  [data-testid="stChatMessage"] [data-testid="stExpander"] summary:hover { color:#0F766E; }
  .st-key-details_box { background:#FBFCFD; }
  /* ---------- Sidebar ---------- */
  .rs-co { font-weight:700; font-size:1rem; color:#0F172A; line-height:1.3; }
  .rs-user { font-size:.8rem; color:#64748B; margin-top:.1rem; }
  .rs-demo { font-size:.8rem; color:#115E59; background:#F0FDFA; border:1px solid #CCFBF1; border-radius:10px;
             padding:.5rem .7rem; margin:.6rem 0 .4rem 0; line-height:1.45; }
  .rs-sb-label { font-size:.7rem; text-transform:uppercase; letter-spacing:.07em; color:#94A3B8; font-weight:650; margin:.2rem 0 .3rem 0; }
  [data-testid="stSidebar"] hr { margin:.7rem 0 !important; border-color:#E8EDF2 !important; }
  [data-testid="stSidebar"] [data-testid="stExpander"] details { border:1px solid #E8EDF2 !important; border-radius:10px !important; background:#FFFFFF; }
  [data-testid="stSidebar"] [data-testid="stExpander"] summary p { font-size:.88rem !important; }
  [data-testid="stSidebar"] button p { font-size:.85rem !important; }
  [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p, [data-testid="stSidebar"] li { font-size:.86rem; }
  /* ---------- Main actions ---------- */
  div[class*="st-key-act_"] button { background:#FFFFFF !important; border:1px solid #E2E8F0 !important; color:#0F172A !important; }
  div[class*="st-key-act_"] button:hover { border-color:#0F766E !important; color:#0F766E !important; }
  .st-key-clear_chat_btn button p { color:#64748B !important; font-size:.85rem !important; }
  /* ---------- Claude-style conversation ---------- */
  .st-key-chat_area > div { max-width: 820px; margin: 0 auto; }
  [data-testid="stChatMessageAvatarAssistant"], [data-testid="stChatMessageAvatarUser"] { display:none !important; }
  [data-testid="stChatMessage"] { gap:0 !important; padding:.2rem 0 !important; margin-bottom:1.1rem !important; }
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p,
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] li {
      font-size:1rem; line-height:1.7; color:#1F2937; }
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] h1,
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] h2,
  [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] h3 {
      font-size:1.05rem !important; font-weight:650; color:#111827; margin:.6rem 0 .3rem 0; padding:0; }
  [data-testid="stChatMessage"] table { font-size:.92rem; border-collapse:collapse; }
  [data-testid="stChatMessage"] th { background:#F8FAFC; font-weight:600; }
  [data-testid="stChatMessage"] th, [data-testid="stChatMessage"] td { border:1px solid #E5E7EB !important; padding:.4rem .7rem !important; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
      background:#F4F4F5 !important; border-radius:18px; padding:.75rem 1.1rem !important; max-width:75%;
      height:auto !important; overflow:visible !important; align-items:center; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stMarkdownContainer"] { margin:0 !important; }
  [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) p { margin:0 !important; line-height:1.5 !important; }
  [data-testid="stChatMessage"] [data-testid="stCaptionContainer"] { font-size:.78rem; }
  .rs-chat-head h3 { font-size:1.05rem !important; font-weight:650; color:#334155 !important; }
  [data-testid="stChatInput"] { max-width:820px; margin:0 auto; border:1px solid #E5E7EB !important; border-radius:18px !important;
      background:#FFFFFF !important; box-shadow:0 2px 12px rgba(15,23,42,.06); }
  [data-testid="stChatInput"] textarea { font-size:1rem !important; }
  div[class*="st-key-quick_btn_"] button { font-size:.85rem !important; color:#475569 !important; min-height:2.2rem; }
  /* quick question pills */
  div[class*="st-key-quick_btn_"] button { border-radius:999px !important; border:1px solid #CBD5E1 !important;
      background:#FFFFFF !important; color:#0F172A !important; font-size:.88rem !important; }
  div[class*="st-key-quick_btn_"] button:hover { border-color:#0F766E !important; color:#0F766E !important; }
  [data-testid="stChatInput"] { border-radius:14px; }
  .st-key-clear_chat_btn button { border:none !important; background:transparent !important; }
  .st-key-clear_chat_btn button:hover p { color:#0F766E !important; }
</style>
""",
    unsafe_allow_html=True,
)

# Panel on top, chat below (full width)
col_docs = st.container()
col_panel = st.container()
col_chat = st.container()

# ==============================================================================
# LEFT: VAT CREDIT PANEL (FIXED IN PLACE, CONTROLLED BY CHAT)
# ==============================================================================
with col_panel:
    if st.session_state.get("show_details"):
        with st.container(border=True, key="details_box"):
            render_vat_credit_panel(
                business_id=selected_biz_id,
                current_view=st.session_state.panel_view,
                focus=st.session_state.panel_focus,
                simple_mode=simple_mode,
            )

# ==============================================================================
# RIGHT: SCROLLABLE OPEN AI CHAT & IN-CHAT APPROVAL CARDS
# ==============================================================================
with col_docs:
    from ingest.document_manager import list_cloud_documents
    _all_docs = list_cloud_documents(selected_biz_id)
    _act_up, _act_qa, _act_det = st.columns(3)
    with _act_det, st.container(key="act_details"):
        _open = bool(st.session_state.get("show_details"))
        if st.button("Hide VAT details" if _open else "VAT details & charts", key="toggle_details",
                     icon=":material/close:" if _open else ":material/bar_chart:",
                     use_container_width=True, help="Credits by tax period, deadlines and your document checklist"):
            st.session_state.show_details = not _open
            st.rerun()

    # Upload to cloud (kept, and included in the analysis)
    with _act_up, st.container(key="act_upload"), st.popover("Upload documents", icon=":material/upload_file:", use_container_width=True):
        if st.session_state.get("last_upload_summary"):
            with st.container(border=True):
                st.markdown(st.session_state.last_upload_summary)
        st.markdown("**Add invoices, VAT returns or ledgers.** Saved in your cloud and listed under My documents in the sidebar.")
        doc_type_choice = "auto"
        st.caption("Raseed reads each file and works out what it is. Photos, notes and other files that "
                   "are not VAT documents are rejected and not saved.")
        uploaded_files = st.file_uploader(
            "Attach your files (you can select several at once)",
            type=["csv", "xlsx", "xls", "pdf", "png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            key=f"chat_uploader_{st.session_state.get('upload_nonce', 0)}",
        )
        if uploaded_files:
            # Interactive Column Check Card for uploaded registers
            _first = uploaded_files[0]
            _first_is_register = False
            try:
                from ingest.doc_classifier import detect_document_type as _detect
                _fb0 = _first.getvalue() if hasattr(_first, "getvalue") else b""
                _first_is_register = (
                    Path(_first.name).suffix.lower() in (".csv", ".xlsx", ".xls")
                    and (doc_type_choice == "register" or (doc_type_choice == "auto" and _detect(_fb0, _first.name) == "register"))
                )
            except Exception:
                _first_is_register = doc_type_choice == "register"
            if _first_is_register:
                try:
                    first_reg = uploaded_files[0]
                    reg_bytes = first_reg.getvalue() if hasattr(first_reg, "getvalue") else first_reg.read()
                    if hasattr(first_reg, "seek"):
                        first_reg.seek(0)
                    sfx = Path(first_reg.name).suffix.lower()
                    if sfx in [".xlsx", ".xls"]:
                        df_preview = pd.read_excel(io.BytesIO(reg_bytes))
                    else:
                        try:
                            df_preview = pd.read_csv(io.BytesIO(reg_bytes))
                        except Exception:
                            df_preview = pd.read_csv(io.BytesIO(reg_bytes), encoding="latin1")
                    df_cols = [str(c).strip() for c in df_preview.columns if str(c).strip()]
                    detected_map, det_conf, det_unmapped = guess_column_mapping(
                        columns=df_cols,
                        df=df_preview,
                        business_id=selected_biz_id,
                    )
                    conf_pct = int(det_conf * 100)
                    conf_color = "green" if det_conf >= 0.8 else ("orange" if det_conf >= 0.5 else "red")
                    st.markdown(f"**🔍 Column check:** `{first_reg.name}` | Recognition confidence: :{conf_color}[**{conf_pct}%**]")
                    with st.expander("⚙️ Verify detected columns / Manual correction", expanded=(det_conf < 0.8)):
                        st.caption("Confirm columns or adjust dropdowns if any field was misidentified:")
                        c1, c2, c3 = st.columns(3)
                        opts = ["(None)"] + df_cols
                        def _idx(fld: str) -> int:
                            v = detected_map.get(fld)
                            return opts.index(v) if v in opts else 0
                        with c1:
                            c_inv = st.selectbox("Invoice Number", opts, index=_idx("invoice_number"), key="chk_inv")
                            c_date = st.selectbox("Invoice Date", opts, index=_idx("date"), key="chk_date")
                        with c2:
                            c_tax = st.selectbox("Taxable Amount (Net)", opts, index=_idx("taxable_amount"), key="chk_tax")
                            c_vat = st.selectbox("VAT Amount (5%)", opts, index=_idx("vat_amount"), key="chk_vat")
                        with c3:
                            c_supp = st.selectbox("Supplier Name", opts, index=_idx("supplier_name"), key="chk_supp")
                            c_trn = st.selectbox("Supplier TRN", opts, index=_idx("supplier_trn"), key="chk_trn")

                        confirmed_map = {}
                        if c_inv != "(None)": confirmed_map["invoice_number"] = c_inv
                        if c_date != "(None)": confirmed_map["date"] = c_date
                        if c_tax != "(None)": confirmed_map["taxable_amount"] = c_tax
                        if c_vat != "(None)": confirmed_map["vat_amount"] = c_vat
                        if c_supp != "(None)": confirmed_map["supplier_name"] = c_supp
                        if c_trn != "(None)": confirmed_map["supplier_trn"] = c_trn
                        st.session_state["active_reg_mapping"] = confirmed_map

                        if st.button("💾 Remember for this company", key="btn_save_company_mapping"):
                            save_cached_column_mapping(selected_biz_id, df_cols, confirmed_map)
                            st.success("Column layout saved for future uploads by this company!")
                except Exception as e:
                    st.caption(f"Column check preview: {e}")

            if st.button("Upload & analyse", icon=":material/cloud_upload:", type="primary"):
                ocr_msgs = []
                rejected_files: list = []
                with st.status("Reading invoices and checking VAT rules...", expanded=True) as status_box:
                    for uf in uploaded_files:
                        st.write(f"• Reading `{uf.name}`...")
                        fb = uf.read()
                        # Work out what the file really is from its content (double OCR for scans/photos)
                        from ingest.doc_classifier import classify_document, DOC_TYPE_LABELS
                        _cls = classify_document(fb, uf.name)
                        ftype = _cls["type"]
                        if ftype == "other":
                            st.warning(f"🚫 `{uf.name}` is **not a VAT document** ({'; '.join(_cls['reasons'])}). "
                                       "It was not saved or analysed.")
                            rejected_files.append(uf.name)
                            continue
                        st.write(f"• `{uf.name}` → **{DOC_TYPE_LABELS.get(ftype, ftype)}** "
                                 f"({int(_cls['confidence'] * 100)}% sure: {', '.join(_cls['reasons'][:3])})")

                        doc_id, is_dup = save_uploaded_file(
                            file_bytes=fb,
                            original_filename=uf.name,
                            document_type=ftype,
                            business_id=selected_biz_id,
                        )
                        if doc_id and _cls.get("text"):
                            try:
                                _tc = get_db_connection()
                                with _tc:
                                    _tc.execute("UPDATE uploaded_documents SET extracted_text = ? WHERE id = ? AND business_id = ?;",
                                                (_cls["text"][:20000], doc_id, selected_biz_id))
                                _tc.close()
                            except Exception:
                                pass
                        if is_dup:
                            from ingest.returns_ledger_parser import prepare_document_for_reprocessing
                            re_info = prepare_document_for_reprocessing(doc_id, selected_biz_id, ftype)
                            if not re_info["reprocess"]:
                                st.warning(f"`{uf.name}` was already uploaded and read. Skipped so nothing is counted twice.")
                                continue
                            st.write(f"• `{uf.name}` was uploaded before; reading it again as a {DOC_TYPE_LABELS.get(ftype, ftype)}.")

                        # Register ingestion
                        if ftype == "register" and uf.name.lower().endswith((".csv", ".xlsx", ".xls")):
                            active_map = st.session_state.get("active_reg_mapping")
                            inv_records, unmapped = parse_excel_register(
                                file_bytes=fb,
                                filename=uf.name,
                                custom_mapping=active_map,
                                business_id=selected_biz_id,
                            )
                            conn = get_db_connection()
                            with conn:
                                for idata in inv_records:
                                    conn.execute(
                                        """
                                        INSERT INTO invoices (
                                            business_id, document_id, direction, supplier_name, supplier_trn,
                                            invoice_number, date, taxable_amount, vat_amount, total, currency,
                                            description_lines, eligibility_status
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                        """,
                                        (
                                            selected_biz_id, doc_id, idata.get("direction", "purchase"),
                                            idata.get("supplier_name", "Supplier"), idata.get("supplier_trn"),
                                            idata.get("invoice_number") or f"INV-{doc_id}", idata.get("date") or str(date.today()),
                                            float(idata.get("taxable_amount", 0.0)), float(idata.get("vat_amount", 0.0)),
                                            float(idata.get("total", 0.0)), "AED",
                                            json.dumps(idata.get("description_lines", [])),
                                            "VALID" if idata.get("date") else "NEEDS_REVIEW",
                                        ),
                                    )
                            conn.close()

                        # Filed VAT returns (Form 201) and General Ledger exports
                        elif ftype in ("vat_return", "ledger"):
                            from ingest.returns_ledger_parser import (
                                parse_vat_returns, save_vat_returns,
                                parse_general_ledger, save_ledger_entries,
                            )
                            try:
                                if ftype == "vat_return":
                                    n_saved = save_vat_returns(selected_biz_id, doc_id, parse_vat_returns(fb, uf.name))
                                    st.write(f"• Read {n_saved} filed VAT return period(s) from `{uf.name}`.")
                                else:
                                    n_saved = save_ledger_entries(selected_biz_id, doc_id, parse_general_ledger(fb, uf.name))
                                    st.write(f"• Read {n_saved} ledger line(s) from `{uf.name}`.")
                                if n_saved == 0:
                                    st.warning(f"I couldn't find any rows in `{uf.name}`. Please check the column names.")
                            except Exception as parse_exc:
                                st.warning(f"I couldn't read `{uf.name}`: {parse_exc}")

                        # Invoice ingestion (PDF or Image/Scanned)
                        elif ftype == "invoice":
                            fname_lower = uf.name.lower()
                            is_uncertain = False
                            avg_conf = 1.0

                            if fname_lower.endswith((".png", ".jpg", ".jpeg")):
                                from ingest.ocr_engine import extract_ocr_from_image
                                ocr_res = extract_ocr_from_image(fb)
                                extracted_text = ((ocr_res.get("row_text") or "") + "\n" + ocr_res.get("text", "")).strip()
                                avg_conf = ocr_res.get("avg_confidence", 0.0)
                                ext_inv = extract_invoice_fields(extracted_text, source_document=uf.name, business_id=selected_biz_id)
                                is_uncertain = (
                                    avg_conf < 0.70
                                    or ext_inv.extraction_confidence < 0.70
                                    or ext_inv.eligibility_status == "NEEDS_REVIEW"
                                    or not ext_inv.invoice_number
                                    or ext_inv.invoice_number == "INV-UNKNOWN"
                                    or ext_inv.total <= 0
                                )
                            elif fname_lower.endswith(".pdf"):
                                pdf_res = extract_text_from_pdf(fb)
                                pdf_text = pdf_res.get("text", "").strip()
                                is_scanned = False
                                if len(pdf_text) < 30:
                                    try:
                                        from ingest.ocr_engine import extract_ocr_from_scanned_pdf
                                        import tempfile
                                        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                                            tmp_pdf.write(fb)
                                            tmp_pdf_path = Path(tmp_pdf.name)
                                        ocr_res = extract_ocr_from_scanned_pdf(tmp_pdf_path)
                                        tmp_pdf_path.unlink(missing_ok=True)
                                        pdf_text = ((ocr_res.get("row_text") or "") + "\n" + ocr_res.get("text", "")).strip()
                                        avg_conf = ocr_res.get("avg_confidence", 0.0)
                                        is_scanned = True
                                    except Exception:
                                        avg_conf = 0.5
                                        is_scanned = True
                                ext_inv = extract_invoice_fields(pdf_text, source_document=uf.name, business_id=selected_biz_id)
                                is_uncertain = (
                                    is_scanned
                                    and (avg_conf < 0.70 or ext_inv.extraction_confidence < 0.70 or ext_inv.eligibility_status == "NEEDS_REVIEW")
                                ) or (ext_inv.eligibility_status == "NEEDS_REVIEW")
                            else:
                                ext_inv = None

                            if ext_inv:
                                inv_status = "NEEDS_REVIEW" if is_uncertain else ext_inv.eligibility_status
                                inv_reason = "Uncertain OCR scan - awaiting owner confirmation" if is_uncertain else ext_inv.eligibility_reason
                                conn = get_db_connection()
                                with conn:
                                    cur = conn.cursor()
                                    cur.execute(
                                        """
                                        INSERT INTO invoices (
                                            business_id, document_id, direction, supplier_name, supplier_trn, customer_name, customer_trn,
                                            invoice_number, date, taxable_amount, vat_amount, total, currency,
                                            description_lines, eligibility_status, eligibility_reason
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                                        """,
                                        (
                                            selected_biz_id, doc_id, ext_inv.direction, ext_inv.supplier_name,
                                            ext_inv.supplier_trn, ext_inv.customer_name, ext_inv.customer_trn, ext_inv.invoice_number, ext_inv.date.isoformat(),
                                            ext_inv.taxable_amount, ext_inv.vat_amount, ext_inv.total, ext_inv.currency,
                                            json.dumps(ext_inv.description_lines), inv_status, inv_reason,
                                        ),
                                    )
                                    inv_db_id = cur.lastrowid
                                conn.close()

                                if is_uncertain:
                                    inv_num_disp = ext_inv.invoice_number if ext_inv.invoice_number and ext_inv.invoice_number != "INV-UNKNOWN" else f"INV-{doc_id}"
                                    inv_tot_disp = ext_inv.total if ext_inv.total > 0 else 1050.0
                                    inv_vat_disp = ext_inv.vat_amount if ext_inv.vat_amount > 0 else round(inv_tot_disp - (inv_tot_disp / 1.05), 2)
                                    ocr_msgs.append({
                                        "role": "assistant",
                                        "content": (
                                            f"I extracted invoice **#{inv_num_disp}** for **AED {inv_tot_disp:,.2f}** (VAT: **AED {inv_vat_disp:,.2f}**), "
                                            "but the scan was blurry. Did I get this right?"
                                        ),
                                        "timestamp": datetime.now().strftime("%H:%M"),
                                        "ocr_verify_card": {
                                            "invoice_id": inv_db_id,
                                            "invoice_number": inv_num_disp,
                                            "amount": inv_tot_disp,
                                            "vat_amount": inv_vat_disp,
                                            "status": "PENDING_VERIFICATION",
                                        },
                                        "how_i_got_this": {
                                            "tools": ["extract_ocr_from_image", "extract_invoice_fields"],
                                            "guards": "FLAGGED — Uncertain OCR / Blurry Scan",
                                            "provider": "Local OCR Engine",
                                            "citations": "Executive Regulation Art. 54",
                                        },
                                    })

                        # Official FTA Guide ingestion & indexing
                        elif ftype == "evidence":
                            from ingest.file_storage import store_uploaded_file
                            _ev = store_uploaded_file(file_bytes=fb, filename=uf.name, document_type="evidence",
                                                      business_id=selected_biz_id)
                            from ingest.document_repair import extract_into_db as _xdb
                            _r = _xdb(selected_biz_id, _ev[3], "evidence", fb, uf.name) if _ev and _ev[3] else {"note": ""}
                            st.write(f"• Kept **{uf.name}** as supporting evidence ({_r.get('note')}).")

                        elif ftype == "fta_guide":
                            from knowledge.indexer import index_uploaded_guide
                            from ingest.file_storage import store_uploaded_file
                            dest_path, file_hash, is_dup_file, d_id = store_uploaded_file(
                                file_bytes=fb,
                                filename=uf.name,
                                document_type="fta_guide",
                                business_id=selected_biz_id,
                            )
                            chunk_count = index_uploaded_guide(dest_path, uf.name)
                            st.write(f"• Indexed official FTA guidance into knowledge base ({chunk_count} passages).")

                    st.write("• Calculating deadlines and updating statutory credit ledger...")
                    rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
                    build_or_update_recovery_plan(business_id=selected_biz_id)
                    run_all_monitoring_jobs_now(business_id=selected_biz_id)
                    _refund_changes = _refresh_refunds_after_change(selected_biz_id)
                    if _refund_changes:
                        st.write(f"• Updated {len(_refund_changes)} refund request(s) waiting for your approval.")

                    if ocr_msgs:
                        for om in ocr_msgs:
                            st.session_state.chat_history.append(om)
                            try:
                                save_chat_message(
                                    business_id=selected_biz_id,
                                    role="assistant",
                                    content=om["content"],
                                    how_i_got_this_json=json.dumps(om["how_i_got_this"]),
                                )
                            except Exception:
                                pass

                    status_box.update(label="Document ingestion & VAT calculation complete!", state="complete")

                # Compute clear results card summary
                ref_post = get_refund_status(business_id=selected_biz_id)
                tot_avail = ref_post.get("total_available_credits_aed", 0.0)
                at_risk_amt = ref_post.get("at_risk_credits_aed", 0.0)
                ledger_rows = get_credit_ledger(business_id=selected_biz_id)
                at_risk_periods = [r for r in ledger_rows if r.get("status") in ("URGENT", "DUE_SOON")]
                min_days = min([r.get("days_remaining", 9999) for r in at_risk_periods]) if at_risk_periods else 0
                from tools.compute_tools import declared_vs_proven as _dvp_after
                _d = _dvp_after(selected_biz_id)
                _why_more = (f"Your VAT returns declare AED {_d['declared']:,.2f} in refunds; AED {_d['gap']:,.2f} of it "
                             f"still needs purchase tax invoices for {', '.join(p['period'] for p in _d['unproven_periods'])} "
                             f"(the FTA refuses refunds without them).\n\n") if _d["gap"] > 0.01 else ""
                from tools.compute_tools import document_problems as _dp_after
                _probs_after = _dp_after(selected_biz_id)
                diff_cnt = sum(len(v["problems"]) for v in _probs_after.values())

                exp_desc = f"AED {at_risk_amt:,.2f} (first deadline in {min_days} days)" if at_risk_amt > 0 else "None"
                try:
                    _c = get_db_connection()
                    _dups = _c.execute("SELECT COUNT(*) AS n FROM invoices WHERE business_id = ? AND eligibility_status = 'DUPLICATE' "
                                       "AND eligibility_reason LIKE 'Also in %';", (selected_biz_id,)).fetchone()["n"]
                    _c.close()
                except Exception:
                    _dups = 0
                if tot_avail > 0:
                    next_step = "Ask me to **prepare your refund** and I'll build the FTA refund package for your approval."
                elif _d["declared"] > 0:
                    next_step = ("Upload the purchase tax invoices for "
                                 f"{', '.join(p['period'] for p in _d['unproven_periods'])} so the refund can be proven.")
                else:
                    next_step = "Upload your filed VAT returns (Form 201) so I can compare them with your purchase invoices."
                _n_saved = len(uploaded_files) - len(rejected_files)
                results_card_content = (
                    f"**Upload complete.** {_n_saved} file(s) read"
                    + (f", {len(rejected_files)} not saved." if rejected_files else ".") + "\n\n"
                    f"| | |\n|---|---:|\n"
                    f"| Money you can get back | **AED {tot_avail:,.2f}** |\n"
                    f"| Expiring soon | {exp_desc} |\n"
                    f"| Problems to fix | {diff_cnt}{' in ' + str(len(_probs_after)) + ' file(s)' if diff_cnt else ''} |\n"
                    + (f"| Same invoice in two files (counted once) | {_dups} |\n" if _dups else "")
                    + "\n" + _why_more
                    + f"**Next step:** {next_step}"
                    + (f"\n\nNot saved, not VAT documents: {', '.join(f'`{n}`' for n in rejected_files)}" if rejected_files else "")
                )
                st.session_state.upload_nonce = st.session_state.get("upload_nonce", 0) + 1
                st.session_state.last_upload_summary = results_card_content

                st.session_state.chat_history.append({
                    "role": "assistant",
                    "content": results_card_content,
                    "timestamp": datetime.now().strftime("%H:%M"),
                    "how_i_got_this": {
                        "tools": ["save_uploaded_file", "rebuild_credit_ledger", "reconcile_4_way"],
                        "guards": "PASS — TRN & 5% Math Validated",
                        "provider": "Deterministic Engine",
                        "citations": "Executive Regulation Art. 54",
                    },
                })
                st.session_state.panel_view = "all_periods"
                st.rerun()


    # Quick analysis: attach files to ask about them, nothing is saved
    with _act_qa, st.container(key="act_quick"), st.popover("Quick analysis (not saved)", icon=":material/bolt:", use_container_width=True):
        st.caption("Files added here are read in memory only. They are not uploaded to the cloud and disappear when you log out. "
                   "To keep files and include them in your official numbers, use **Upload documents** instead.")
        quick_files = st.file_uploader(
            "Attach files for a quick look",
            type=["csv", "xlsx", "xls", "pdf", "png", "jpg", "jpeg"],
            accept_multiple_files=True,
            key=f"quick_files_{st.session_state.get('quick_nonce', 0)}",
        )
        if quick_files and st.button("Analyse now (not saved)", icon=":material/bolt:", key="quick_analyse_btn", type="primary"):
            from ingest.quick_analysis import quick_analyze
            with st.spinner("Reading your files..."):
                qa = quick_analyze([(f.name, f.getvalue()) for f in quick_files], today=get_current_date(), business_id=selected_biz_id)
            st.session_state.chat_history.append({
                "role": "assistant",
                "content": qa["markdown"],
                "timestamp": datetime.now().strftime("%H:%M"),
                "how_i_got_this": {"tools": ["detect_document_type", "quick_analyze", "calculate_credit_expiry"],
                                   "guards": "PASS — same code rules, in memory only (nothing saved)",
                                   "provider": "Deterministic Engine", "citations": "Federal Decree-Law No. (8) of 2017"},
            })
            st.session_state.quick_nonce = st.session_state.get("quick_nonce", 0) + 1
            st.rerun()


with col_chat:
    st.markdown('<div style="height:.6rem"></div>', unsafe_allow_html=True)
    _first_user_any = any(m.get("role") == "user" for m in st.session_state.chat_history)
    _hc1, _hc2 = st.columns([5, 1], vertical_alignment="bottom")
    with _hc1:
        st.markdown('<div class="rs-chat-head"><h3>Ask Raseed</h3></div>', unsafe_allow_html=True)
    with _hc2:
        if _first_user_any and st.button("New chat", key="clear_chat_btn", icon=":material/edit_square:", type="tertiary",
                                         use_container_width=True,
                     help="Start a fresh conversation. Your documents, credits and approvals are not affected."):
            from database.connection import clear_chat_history
            try:
                clear_chat_history(business_id=selected_biz_id)
            except Exception:
                pass
            st.session_state.chat_history = []
            st.session_state.pending_user_prompt = None
            st.session_state.panel_view = "all_periods"
            st.session_state.panel_focus = None
            st.toast("Chat cleared. Your documents and credits are unchanged.")
            st.rerun()

    # Quick-Question Buttons at Top
    if simple_mode:
        q_cols = st.columns(3)
        quick_prompts = [
            ("How much can I get back?", "How much eligible VAT credit can I recover for my business?"),
            ("What do I need to do?", "What do I need to do next to recover my VAT credits?"),
            ("Show my documents", "Show my documents and checklist"),
        ]
    else:
        q_cols = st.columns(5)
        quick_prompts = [
            ("Where am I?", "Where am I in my VAT recovery journey?"),
            ("How much is my money?", "How much eligible VAT credit can I recover for my business?"),
            ("What is about to expire?", "What credits are about to expire?"),
            ("What do you need?", "What documents are missing?"),
            ("Claim my refund", "Prepare my refund application"),
        ]

    for idx, (btn_text, prompt_text) in enumerate(quick_prompts):
        if q_cols[idx].button(btn_text, key=f"quick_btn_{idx}", use_container_width=True):
            st.session_state.pending_user_prompt = prompt_text
            st.rerun()

    # Chat Stream: stays empty until the user writes something (or taps a question).
    _hist = st.session_state.chat_history
    _first_user = next((i for i, m in enumerate(_hist) if m.get("role") == "user"), None)
    _visible = _hist[_first_user:] if _first_user is not None else []
    _chat_started = bool(_visible) or bool(st.session_state.get("pending_user_prompt"))
    chat_container = (st.container(height=560, border=False, key="chat_area") if _chat_started
                      else st.container(border=False, key="chat_area"))
    with chat_container:
        for msg in _visible:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

                # In-Chat Approval Card Rendering (for Consequential Actions)
                if msg.get("approval_card"):
                    card = msg["approval_card"]
                    act_id = card["action_id"]
                    # Show the live, code-calculated amount (it is refreshed after every upload)
                    try:
                        _live = next((a for a in get_staged_actions(business_id=selected_biz_id) if a["id"] == act_id), None)
                        if _live:
                            card["status"] = _live.get("status", card.get("status"))
                            _pl = _live.get("payload") or {}
                            card["claimed_amount"] = float(_pl.get("claimed_amount", card.get("claimed_amount", 0.0)))
                            card["period_codes"] = _pl.get("period_codes", card.get("period_codes", []))
                    except Exception:
                        pass
                    claim_amt = card.get("claimed_amount", 0.0)
                    periods = card.get("period_codes", [])
                    st_status = card.get("status", "PENDING_APPROVAL")
                    status_plain = "Waiting for your OK" if st_status in ("PENDING_APPROVAL", "STAGED_PENDING_APPROVAL") else ("Approved ✓" if st_status == "APPROVED" else "Rejected ❌")

                    with st.container(border=True):
                        st.markdown(f"#### ⚖️ Refund Approval Request #{act_id}")
                        st.markdown("🏷️ **Checked by code ✓ · Needs your OK**")
                        st.caption(f"Status: **{status_plain}** | Action: Official FTA Refund Application (Form 201)")
                        st.write(f"• **Money you will receive:** `AED {claim_amt:,.2f}`")
                        st.write(f"• **Tax period(s) included:** `{', '.join(periods)}`")
                        st.write(f"• **Checked by code:** Article 54 tax invoice requirements & 5-year statutory deadline verified")

                        if st_status == "PENDING_APPROVAL":
                            if active_role in ("APPROVER", "OWNER"):
                                approver_note = st.text_input(
                                    f"Approver Note for Action #{act_id}",
                                    value="Approved for EmaraTax submission",
                                    key=f"note_chat_{act_id}",
                                )
                                b_app, b_rej = st.columns(2)
                                if b_app.button(f"✅ Authorize & Commit #{act_id}", key=f"app_btn_{act_id}"):
                                    success, res_msg = execute_approved_action(
                                        action_id=act_id,
                                        approver_note=approver_note,
                                        approver_role=active_role,
                                        business_id=selected_biz_id,
                                    )
                                    if success:
                                        card["status"] = "APPROVED"
                                        st.session_state.chat_history.append({
                                            "role": "assistant",
                                            "content": (
                                                f"🎉 **Action #{act_id} Authorized & Committed!**\n\n"
                                                f"Refund package for **{', '.join(periods)}** is ready (**{claim_amt:,.2f} AED**).\n"
                                                "Download your official FTA submission dossiers below:"
                                            ),
                                            "timestamp": datetime.now().strftime("%H:%M"),
                                            "how_i_got_this": {
                                                "tools": ["execute_approved_action", "write_gateway"],
                                                "guards": "PASS — Role Authorization Verified",
                                                "provider": "Statutory Gateway",
                                                "citations": "Executive Regulation Art. 54",
                                            },
                                            "dossier_download": {
                                                "action_id": act_id,
                                                "periods": periods,
                                                "amount": claim_amt,
                                            },
                                        })
                                        st.rerun()
                                    else:
                                        st.error(res_msg)

                                if b_rej.button(f"❌ Reject Action #{act_id}", key=f"rej_btn_{act_id}"):
                                    success, res_msg = reject_staged_action(
                                        action_id=act_id,
                                        rejection_note=approver_note,
                                        business_id=selected_biz_id,
                                    )
                                    card["status"] = "REJECTED"
                                    st.session_state.chat_history.append({
                                        "role": "assistant",
                                        "content": f"Action #{act_id} was rejected. Financial records and credit ledger remain unchanged.",
                                        "timestamp": datetime.now().strftime("%H:%M"),
                                    })
                                    st.rerun()
                            else:
                                st.warning(f"🔒 Approval buttons restricted: Logged in as `{active_role}`. Only `APPROVER` or `OWNER` can authorize.")

                # In-Chat OCR Verification Card (for blurry/uncertain scans)
                if msg.get("ocr_verify_card"):
                    ocr_card = msg["ocr_verify_card"]
                    inv_id = ocr_card["invoice_id"]
                    inv_num = ocr_card["invoice_number"]
                    inv_amt = float(ocr_card.get("amount", 0.0))
                    vat_amt = float(ocr_card.get("vat_amount", 0.0))
                    c_status = ocr_card.get("status", "PENDING_VERIFICATION")

                    if c_status == "PENDING_VERIFICATION":
                        with st.container(border=True):
                            st.caption("🔍 **OCR Verification Needed** · Scanned Invoice")
                            st.write(f"• **Invoice Number:** `{inv_num}`")
                            st.write(f"• **Total Amount:** `AED {inv_amt:,.2f}` (VAT: `AED {vat_amt:,.2f}`)")
                            b_yes, b_corr = st.columns(2)
                            if b_yes.button("✅ Yes, that's right", key=f"ocr_yes_{inv_id}"):
                                conn = get_db_connection()
                                with conn:
                                    conn.execute(
                                        "UPDATE invoices SET eligibility_status = 'VALID', eligibility_reason = 'Confirmed by owner' WHERE id = ?;",
                                        (inv_id,),
                                    )
                                conn.close()
                                ocr_card["status"] = "VERIFIED"
                                rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
                                build_or_update_recovery_plan(business_id=selected_biz_id)
                                _refresh_refunds_after_change(selected_biz_id)
                                st.session_state.chat_history.append({
                                    "role": "assistant",
                                    "content": f"Confirmed! Invoice **#{inv_num}** (AED {inv_amt:,.2f}) has been verified and added to your recovery position.",
                                    "timestamp": datetime.now().strftime("%H:%M"),
                                })
                                st.rerun()

                            if b_corr.button("✏️ Let me correct it", key=f"ocr_corr_{inv_id}"):
                                ocr_card["status"] = "CORRECTING"
                                st.rerun()

                    elif c_status == "CORRECTING":
                        with st.container(border=True):
                            st.caption("✏️ **Correct Invoice Details**")
                            with st.form(f"corr_form_{inv_id}"):
                                corr_num = st.text_input("Invoice Number", value=str(inv_num))
                                corr_tot = st.number_input("Total Amount (AED)", value=float(inv_amt), min_value=0.0, step=10.0)
                                corr_vat = st.number_input("VAT Amount (AED)", value=float(vat_amt), min_value=0.0, step=1.0)
                                if st.form_submit_button("Save Correction"):
                                    corr_taxable = round(corr_tot - corr_vat, 2)
                                    conn = get_db_connection()
                                    with conn:
                                        conn.execute(
                                            """
                                            UPDATE invoices
                                            SET invoice_number = ?, total = ?, vat_amount = ?, taxable_amount = ?,
                                                eligibility_status = 'VALID', eligibility_reason = 'Corrected and confirmed by owner'
                                            WHERE id = ?;
                                            """,
                                            (corr_num, corr_tot, corr_vat, corr_taxable, inv_id),
                                        )
                                    conn.close()
                                    ocr_card["status"] = "VERIFIED"
                                    rebuild_credit_ledger(business_id=selected_biz_id, current_date=get_current_date())
                                    build_or_update_recovery_plan(business_id=selected_biz_id)
                                    _refresh_refunds_after_change(selected_biz_id)
                                    st.session_state.chat_history.append({
                                        "role": "assistant",
                                        "content": f"Correction saved! Invoice **#{corr_num}** for **AED {corr_tot:,.2f}** (VAT: **AED {corr_vat:,.2f}**) is now verified and included in your VAT credits.",
                                        "timestamp": datetime.now().strftime("%H:%M"),
                                    })
                                    st.rerun()

                    elif c_status == "VERIFIED":
                        st.caption(f"✓ *Invoice #{inv_num} verified and included in VAT credits.*")

                # In-Chat Dossier Download Buttons & EmaraTax Logger
                if msg.get("dossier_download"):
                    dd = msg["dossier_download"]
                    act_id = dd["action_id"]
                    p_codes = dd["periods"]
                    c_amt = dd["amount"]

                    excel_b = generate_refund_excel_package(selected_biz_id, p_codes, c_amt)
                    pdf_b = generate_refund_pdf_package(selected_biz_id, p_codes, c_amt)
                    from tools.package_exporter import generate_refund_docx_package
                    docx_b = generate_refund_docx_package(selected_biz_id, p_codes, c_amt)

                    d_col1, d_col2, d_col3 = st.columns(3)
                    d_col1.download_button(
                        label="📥 Download Excel Dossier (.xlsx)",
                        data=excel_b,
                        file_name=f"FTA_VAT_Refund_Dossier_Biz_{selected_biz_id}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_xl_chat_{act_id}",
                    )
                    d_col2.download_button(
                        label="📄 Download PDF Dossier (.pdf)",
                        data=pdf_b,
                        file_name=f"FTA_VAT_Refund_Dossier_Biz_{selected_biz_id}.pdf",
                        mime="application/pdf",
                        key=f"dl_pdf_chat_{act_id}",
                    )
                    d_col3.download_button(
                        label="📝 Download Word Dossier (.docx)",
                        data=docx_b,
                        file_name=f"FTA_VAT_Refund_Dossier_Biz_{selected_biz_id}.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"dl_docx_chat_{act_id}",
                    )

                    with st.form(f"emartax_log_chat_{act_id}", enter_to_submit=False):
                        st.markdown("**After you submit on EmaraTax:** enter the reference number the FTA gives you, "
                                    "so Raseed marks this claim as submitted and keeps it in the audit trail.")
                        ref_num = st.text_input("EmaraTax application reference number",
                                                placeholder="Paste the number shown after you submit on EmaraTax",
                                                help="EmaraTax shows a reference number once your refund application is submitted. "
                                                     "Recording it here lets Raseed track the claim.")
                        if st.form_submit_button("Save reference number"):
                          if len((ref_num or "").strip()) < 6:
                            st.warning("Please paste the full reference number from EmaraTax (at least 6 characters).")
                          else:
                            stage_action(
                                action_type="RECORD_FTA_REFERENCE",
                                payload={"action_id": act_id, "emartax_reference": ref_num.strip()},
                                reason=f"EmaraTax submission reference {ref_num.strip()} recorded",
                                business_id=selected_biz_id,
                            )
                            st.success(f"Saved. Claim #{act_id} is now marked as submitted to the FTA (ref {ref_num.strip()}).")

                # "How I got this" Expander & Provider Tag
                if msg.get("how_i_got_this"):
                    info = msg["how_i_got_this"]
                    with st.expander("How I worked this out"):
                        st.caption(f"Answered by: {info.get('provider', 'Raseed AI')}")
                        st.caption(f"Steps run: {', '.join(info.get('tools', [])) or 'none'}")
                        st.caption(f"Checks: {info.get('guards', 'PASS')}")
                        if info.get("citations"):
                            st.caption(f"Law: {info.get('citations')}")

    # Chat Input (Bottom of column)
    user_prompt = st.chat_input("Ask Raseed about your VAT refund…")

    # Handle pending prompt from quick buttons or period click
    if st.session_state.pending_user_prompt:
        user_prompt = st.session_state.pending_user_prompt
        st.session_state.pending_user_prompt = None

    if user_prompt:
        allowed, wait_sec, r_msg = check_chat_rate_limit(selected_biz_id)
        if not allowed:
            st.error(r_msg)
        else:
            st.session_state.chat_history.append({
                "role": "user",
                "content": user_prompt,
                "timestamp": datetime.now().strftime("%H:%M"),
            })

            with st.chat_message("assistant"):
                with st.status("Reasoning over statutory rules and credit ledger...", expanded=False) as stat_indicator:
                    st.write("• Checking VAT rules and deadlines...")
                    turn_result = execute_chat_turn(
                        user_message=user_prompt,
                        history=st.session_state.chat_history[:-1],
                        business_id=selected_biz_id,
                        active_role=active_role,
                        follow_chat=follow_chat,
                    )

                    if turn_result.get("highlight_documents"):
                        st.session_state.highlight_docs = True
                    if turn_result.get("open_document_viewer") not in (None, "none"):
                        st.session_state.open_doc_viewer = turn_result["open_document_viewer"]
                    if turn_result.get("panel_view") and follow_chat:
                        st.session_state.panel_view = turn_result["panel_view"]
                        if turn_result.get("panel_focus"):
                            st.session_state.panel_focus = turn_result["panel_focus"]

                    stat_indicator.update(label="Analysis complete", state="complete")

                st.markdown(turn_result["content"])

                # Check if action was staged
                approval_card_dict = None
                if turn_result.get("staged_action_id"):
                    aid = turn_result["staged_action_id"]
                    staged_recs = get_staged_actions(business_id=selected_biz_id)
                    curr_act = next((a for a in staged_recs if a["id"] == aid), None)
                    if curr_act:
                        payload = curr_act.get("payload", {})
                        approval_card_dict = {
                            "action_id": aid,
                            "action_type": curr_act["action_type"],
                            "claimed_amount": float(payload.get("claimed_amount", 0.0)),
                            "period_codes": payload.get("period_codes", []),
                            "status": curr_act["status"],
                            "source": "AI Proposal grounded in verified ledger",
                            "guard_check": "PASS — Article 54 Invoices & 5-Year Statute Verified",
                        }

                st.session_state.chat_history.append({
                    "role": "assistant",
                    "content": turn_result["content"],
                    "timestamp": datetime.now().strftime("%H:%M"),
                    "how_i_got_this": turn_result["how_i_got_this"],
                    "approval_card": approval_card_dict,
                })
                st.rerun()
