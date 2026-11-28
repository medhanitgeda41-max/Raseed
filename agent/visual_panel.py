"""Visual VAT Credit Panel component built with Plotly and Streamlit.

Renders clear, intuitive financial visualizations for the fixed 50% left panel:
- Your VAT credits (all_periods): timeline/bar chart colored by statutory expiry
- Expiring soon (at_risk): countdown and amounts for periods close to the 5-year deadline
- Raseed's plan (recovery_plan): prioritized opportunities ordered by urgency
- Do the numbers match? (reconciliation): 4-way check across registers, GL, and returns
- Documents (documents): required and recommended document checklists
"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import streamlit as st
import plotly.graph_objects as go

from tools.read_tools import (
    get_credit_ledger,
    get_refund_status,
    list_documents,
    get_invoices,
)
from tools.compute_tools import reconcile_4_way
from onboarding.requirements import onboarding_status
from agent.recovery_planner import build_or_update_recovery_plan


STATUS_COLORS = {
    "OK": "#28a745",          # Green
    "DUE_SOON": "#fd7e14",    # Orange
    "URGENT": "#dc3545",      # Red
    "EXPIRED": "#6c757d",     # Grey
}


def format_date_plain(val: Any) -> str:
    """Format dates uniformly as '15 Jan 2026'."""
    if not val:
        return "Not specified"
    try:
        if isinstance(val, (date, datetime)):
            return val.strftime("%d %b %Y")
        clean = str(val).strip().split()[0]
        dt = datetime.strptime(clean, "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return str(val)


def render_all_periods_view(
    business_id: int,
    focus: Optional[str] = None,
    db_path: Optional[Path] = None,
    simple_mode: bool = False,
):
    """Render Plotly bar chart and summary of all tax periods in the credit ledger."""
    if not simple_mode:
        st.markdown("💡 **Your VAT credits:** All tax periods analyzed from your invoices and returns. Green means safe, orange means expiring soon, grey means expired.")

    ledger = get_credit_ledger(business_id=business_id, db_path=db_path)
    if not ledger:
        st.info("📊 **No VAT credits yet** — upload your purchase list (Excel) or invoices (PDF) to start.")
        return

    periods = [r["period_code"] for r in ledger]
    excess_amts = [float(r["net_excess_aed"]) for r in ledger]
    output_amts = [float(r["output_tax_aed"]) for r in ledger]
    input_amts = [float(r["verified_input_tax_aed"]) for r in ledger]
    statuses = [r["status"] for r in ledger]
    
    # Colors: green = available, orange = expiring soon, grey = expired
    colors = []
    for s in statuses:
        if s == "OK":
            colors.append("#28a745")  # Green
        elif s in ("DUE_SOON", "URGENT"):
            colors.append("#fd7e14")  # Orange
        elif s == "EXPIRED":
            colors.append("#6c757d")  # Grey
        else:
            colors.append("#28a745")

    expiry_dates = [format_date_plain(r["expiry_date"]) for r in ledger]
    days_left = [r["days_remaining"] for r in ledger]

    # Create Plotly Bar Chart
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=periods,
        y=excess_amts,
        marker_color=colors,
        text=[f"AED {a:,.0f}" if a > 0 else "" for a in excess_amts],
        textposition="auto",
        customdata=list(zip(output_amts, input_amts, statuses, expiry_dates, days_left)),
        hovertemplate=(
            "<b>Tax Period: %{x}</b><br>"
            "Money you can get back: AED %{y:,.2f}<br>"
            "VAT on sales: AED %{customdata[0]:,.2f}<br>"
            "VAT paid on purchases: AED %{customdata[1]:,.2f}<br>"
            "Status: %{customdata[2]}<br>"
            "Deadline: %{customdata[3]} (%{customdata[4]} days left)"
            "<extra></extra>"
        ),
    ))

    fig.update_layout(
        title="VAT Credits by Tax Period (AED)" if simple_mode else "Money You Can Get Back by Tax Period (AED)",
        xaxis_title="Tax Period",
        yaxis_title="Recoverable VAT (AED)",
        margin=dict(l=20, r=20, t=40, b=20),
        height=320,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    if simple_mode:
        return

    # Focus inspection / Period selector
    st.markdown("##### 🔍 Inspect a Period")
    selected_period = st.selectbox(
        "Choose a tax period to see details:",
        options=periods,
        index=periods.index(focus) if focus in periods else 0,
        key="period_inspector_select",
    )

    # Render card for selected period
    p_row = next((r for r in ledger if r["period_code"] == selected_period), None)
    if p_row:
        excess = float(p_row["net_excess_aed"])
        out_v = float(p_row["output_tax_aed"])
        inp_v = float(p_row["verified_input_tax_aed"])
        st_tag = p_row["status"]
        badge = "🟢 Safe standing" if st_tag == "OK" else ("🟠 Expiring soon" if st_tag == "DUE_SOON" else ("🔴 Urgent" if st_tag == "URGENT" else "⚪ Expired"))

        with st.container(border=True):
            c1, c2, c3 = st.columns(3)
            c1.metric("VAT on Sales", f"AED {out_v:,.2f}", help="Output VAT: VAT you charged customers on your sales.")
            c2.metric("VAT Paid on Purchases", f"AED {inp_v:,.2f}", help="Input VAT: 5% VAT you paid to suppliers that you can claim back.")
            c3.metric("Money You Can Get Back", f"AED {excess:,.2f}", help="Net excess credit: what the FTA owes your business.")

            st.caption(f"**AED {excess:,.2f}** — VAT you paid on purchases that you can claim back for period {selected_period}")
            st.markdown(f"Status: **{badge}** | Statutory Deadline: **{format_date_plain(p_row['expiry_date'])}** ({p_row['days_remaining']} days left)")

            if p_row.get("transitional_deadline_applicable"):
                st.warning("⚠️ **Special Rule (Decree-Law 17/2025):** Pre-2022 credits must be claimed before **31 Dec 2026** or they are permanently lost.")

            if st.button(f"💬 Ask Raseed about {selected_period}", key=f"ask_p_{selected_period}"):
                st.session_state.pending_user_prompt = f"Tell me about {selected_period}"
                st.rerun()


def render_at_risk_view(business_id: int, db_path: Optional[Path] = None):
    """Render credits approaching statutory expiration sorted by days left."""
    st.markdown("💡 **Expiring soon:** Credits you will lose if you don't claim them before the date shown (5-year statutory rule).")

    ledger = get_credit_ledger(business_id=business_id, db_path=db_path)
    at_risk = [r for r in ledger if r.get("status") in ("URGENT", "DUE_SOON")]
    at_risk.sort(key=lambda x: x.get("days_remaining", 9999))

    if not at_risk:
        st.success("🟢 **No credits are expiring soon** — all your recoverable VAT is in safe standing (>180 days left).")
        return

    st.markdown(f"**Found {len(at_risk)} credit period(s) approaching the statutory deadline:**")

    # Horizontal bar chart of days remaining
    periods = [r["period_code"] for r in at_risk]
    days = [r["days_remaining"] for r in at_risk]
    colors = [STATUS_COLORS["URGENT"] if r["status"] == "URGENT" else STATUS_COLORS["DUE_SOON"] for r in at_risk]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=periods,
        x=days,
        orientation="h",
        marker_color=colors,
        text=[f"{d} days left" for d in days],
        textposition="auto",
        hovertemplate="Period: %{y}<br>Days Remaining: %{x}<extra></extra>",
    ))
    fig.update_layout(
        title="Statutory Deadline Countdown (Days Remaining)",
        xaxis_title="Days Left",
        yaxis_title="Tax Period",
        margin=dict(l=20, r=20, t=40, b=20),
        height=240,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    # Detailed Cards
    for r in at_risk:
        p_code = r["period_code"]
        amt = float(r["net_excess_aed"])
        days_rem = r["days_remaining"]
        exp_d = r["expiry_date"]
        is_urg = r["status"] == "URGENT"
        badge = "🔴 Urgent" if is_urg else "🟠 Expiring soon"

        with st.container(border=True):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                st.markdown(f"**{badge}: Period {p_code}**")
                st.write(f"Money you can get back: **AED {amt:,.2f}**")
                st.caption(f"AED {amt:,.2f} — Must be claimed before **{format_date_plain(exp_d)}** ({days_rem} days left)")
            with col_b:
                if st.button("Claim Refund", key=f"claim_{p_code}"):
                    st.session_state.pending_user_prompt = f"Prepare my refund for {p_code}"
                    st.rerun()


def render_recovery_plan_view(business_id: int, db_path: Optional[Path] = None):
    """Render prioritized statutory tasks with impact amounts and deadlines."""
    st.markdown("💡 **Raseed's plan:** Step-by-step actions prioritized by deadline to make sure you recover every dirham before time runs out.")

    plan = build_or_update_recovery_plan(business_id=business_id, db_path=db_path)
    if not plan:
        st.info("📋 **No pending recovery actions** — all verified credits are balanced and ready.")
        return

    st.markdown(f"**{len(plan)} Prioritized Recovery Action(s):**")
    for s in plan:
        prio = s.get("priority", "MEDIUM")
        badge = "🔴 Urgent" if prio == "URGENT" else ("🟠 Expiring soon" if prio in ("HIGH", "DUE_SOON") else "🔵 Safe standing")
        st_tag = s.get("status", "PENDING")
        st_tag_disp = "Waiting for your OK" if st_tag in ("WAITING_USER", "PENDING_APPROVAL") else ("Needs your check" if st_tag == "NEEDS_REVIEW" else ("Completed ✓" if st_tag == "DONE" else "Ready"))
        icon = "✅" if st_tag == "DONE" else ("⏳" if "Waiting" in st_tag_disp else "⚖️")

        with st.container(border=True):
            st.markdown(f"**{icon} {badge}: {s['description']}**")
            c1, c2 = st.columns(2)
            c1.caption(f"Money at stake: **AED {s.get('amount_aed', 0.0):,.2f}**")
            c2.caption(f"Deadline: **{format_date_plain(s.get('deadline')) if s.get('deadline') else 'Standard 5-Year Rule'}**")
            st.write(f"Status: **{st_tag_disp}**")


def render_reconciliation_view(business_id: int, db_path: Optional[Path] = None):
    """Render 4-way statutory reconciliation report across registers, GL, and returns."""
    st.markdown("💡 **Do the numbers match?** A 4-way check between your invoices, accounting ledger, filed returns, and credit ledger to find missing money.")

    recon = reconcile_4_way(business_id=business_id, db_path=db_path)

    if recon["reconciled"]:
        st.success("✅ **Everything matches!**\n\nYour purchase invoices, accounting books, filed returns, and credit ledger match with zero differences.")
        return

    st.warning(f"⚠️ **{recon['discrepancies_count']} Difference(s) Found Across Your Accounting Records**")

    # Donut chart of discrepancy causes
    causes = recon.get("discrepancy_counts_by_cause", {})
    if causes:
        cause_labels = {
            "UNRECORDED_INVOICE": "Invoice not in return",
            "OVERCLAIM": "Claim exceeds invoice",
            "ARITHMETIC_ERROR": "VAT calculation error",
            "BLOCKED_CATEGORY": "Not claimable by law",
        }
        fig = go.Figure(data=[go.Pie(
            labels=[cause_labels.get(c, c.replace("_", " ").title()) for c in causes.keys()],
            values=list(causes.values()),
            hole=0.45,
            marker_colors=["#dc3545", "#fd7e14", "#007bff", "#6f42c1"],
        )])
        fig.update_layout(
            title="Differences by Accounting Cause",
            margin=dict(l=20, r=20, t=35, b=20),
            height=240,
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)

    # Discrepancy Items
    cause_titles = {
        "UNRECORDED_INVOICE": "Missing Invoice in VAT Return",
        "OVERCLAIM": "Claim Higher Than Invoice Amount",
        "ARITHMETIC_ERROR": "VAT Calculation Discrepancy",
        "BLOCKED_CATEGORY": "Not Claimable (Law Restriction)",
    }
    for d in recon.get("discrepancies", []):
        c_title = cause_titles.get(d['cause'], d['cause'])
        with st.expander(f"⚠️ [{c_title}] {d['description']}"):
            st.write(f"**Difference Amount:** `AED {d.get('amount_aed', 0.0):,.2f}`")
            st.write(f"**Records Compared:** `{d.get('source_pair')}`")
            st.caption(f"Tax Period: `{d.get('period_code') or 'General'}`")


def render_documents_view(business_id: int, db_path: Optional[Path] = None):
    """Render required and recommended documentation checklist and uploaded files."""
    st.markdown("💡 **Documents:** Check which records are uploaded and what Raseed still needs to claim your full refund.")

    ob = onboarding_status(business_id=business_id, db_path=db_path)
    docs = list_documents(business_id=business_id, db_path=db_path)

    st.markdown("##### 📋 Document Checklist")
    req_items = [
        ("Business tax profile (TRN & VAT Registration)", "business_profile"),
        ("Purchase invoices and registers (CSV / Excel / PDF)", "purchase_invoices"),
        ("VAT returns you filed (Form 201)", "vat_returns"),
    ]
    present = ob.get("present", [])
    for title, key in req_items:
        is_ok = key in present
        st.write(f"{'✅' if is_ok else '❌'} **{title}** — *{'Verified ✓' if is_ok else 'Missing (Required)'}*")

    rec_items = [
        ("Accounting general ledger export", "general_ledger"),
        ("Bank statement for refund IBAN verification", "bank_statement"),
        ("Official FTA guidance notes", "fta_guides"),
    ]
    for title, key in rec_items:
        is_ok = key in present
        st.write(f"{'✅' if is_ok else '⚪'} {title} — *{'Uploaded ✓' if is_ok else 'Optional (Recommended)'}*")

    st.markdown("---")
    st.markdown(f"##### 📂 Uploaded Files ({len(docs)} files)")
    if not docs:
        st.caption("No documents yet — upload your purchase list (Excel) or invoices to start.")
    else:
        for doc in docs[:8]:
            st.caption(f"• **{doc['original_filename']}** (`{doc['document_type']}`) — {doc['file_size_bytes']} bytes")


def render_inbox_view(business_id: int, db_path: Optional[Path] = None):
    """Render Raseed's autonomous task inbox categorized by state."""
    st.markdown("💡 **Inbox:** Raseed's autonomous task stream — open actions, items waiting for your input, approval requests, and completed work.")

    from scheduler.monitor import get_inbox_tasks
    inbox = get_inbox_tasks(business_id=business_id, db_path=db_path)

    open_tasks = inbox.get("open", [])
    waiting_user = inbox.get("waiting_user", [])
    waiting_approval = inbox.get("waiting_approval", [])
    done_tasks = inbox.get("done", [])

    tab_open, tab_user, tab_appr, tab_done = st.tabs([
        f"📬 Open ({len(open_tasks)})",
        f"⏳ Waiting for you ({len(waiting_user)})",
        f"⚖️ Waiting for your OK ({len(waiting_approval)})",
        f"✅ Done ({len(done_tasks)})",
    ])

    with tab_open:
        if not open_tasks:
            st.info("No open automated tasks. Raseed's continuous background monitoring is up to date.")
        else:
            for t in open_tasks:
                prio_badge = "🔴 Urgent" if t["priority"] == "URGENT" else ("🟠 High" if t["priority"] == "HIGH" else "🔵 Normal")
                with st.container(border=True):
                    st.markdown(f"**{prio_badge}: {t['description']}**")
                    st.caption(f"Task Type: `{t['task_type']}` | Status: `{t['status']}`")

    with tab_user:
        if not waiting_user:
            st.success("Nothing waiting for you! All required documents and reviews are up to date.")
        else:
            for t in waiting_user:
                prio_badge = "🔴 Urgent" if t["priority"] == "URGENT" else ("🟠 High" if t["priority"] == "HIGH" else "🔵 Normal")
                with st.container(border=True):
                    st.markdown(f"**{prio_badge}: {t['description']}**")
                    st.caption(f"Type: `{t['task_type']}` | Created: {format_date_plain(t['created_at'])}")
                    if st.button(f"Address Task #{t['id']}", key=f"inbox_user_btn_{t['id']}"):
                        st.session_state.pending_user_prompt = f"How can I resolve: {t['description']}?"
                        st.rerun()

    with tab_appr:
        if not waiting_approval:
            st.info("No approval requests waiting. Prepare a refund to generate an authorization request.")
        else:
            for t in waiting_approval:
                prio_badge = "🔴 Urgent" if t["priority"] == "URGENT" else ("🟠 High" if t["priority"] == "HIGH" else "🔵 Normal")
                with st.container(border=True):
                    st.markdown(f"**{prio_badge}: {t['description']}**")
                    st.caption(f"Type: `{t['task_type']}` | Status: **Waiting for your OK**")
                    if st.button(f"Open Approval #{t['id']}", key=f"inbox_appr_btn_{t['id']}"):
                        st.session_state.pending_user_prompt = "Prepare my refund application"
                        st.rerun()

    with tab_done:
        if not done_tasks:
            st.caption("No completed tasks recorded yet.")
        else:
            for t in done_tasks[:8]:
                with st.container(border=True):
                    st.markdown(f"**✓ {t['description']}**")
                    st.caption(f"Status: `{t['status']}` | Updated: {format_date_plain(t['updated_at'])}")


def render_rules_view(business_id: int, db_path: Optional[Path] = None):
    """Render statutory UAE VAT rules and their official FTA verification statuses."""
    from knowledge.retriever import get_rule_verification_statuses

    st.markdown("💡 **Official Tax Rules:** These rules govern your VAT recovery under UAE legislation. Python code calculates and checks every single rule; uploaded FTA guides provide official grounding.")

    rules = get_rule_verification_statuses(db_path=db_path)
    if not rules:
        st.info("No tax rules configured. Check `config/tax_rules.yaml`.")
        return

    verified_cnt = sum(1 for r in rules if r.get("status") == "verified")
    to_verify_cnt = len(rules) - verified_cnt

    m_col1, m_col2, m_col3 = st.columns(3)
    m_col1.metric("Statutory Rules", len(rules))
    m_col2.metric("Verified by FTA Guide", verified_cnt)
    m_col3.metric("To Verify", to_verify_cnt)

    # Check for rules affecting active claim that need verification
    unverified_affecting = [r for r in rules if r.get("status") != "verified" and r.get("affects_claim")]
    if unverified_affecting:
        with st.container(border=True):
            st.markdown(
                f"⚠️ **Escalation Notice:** {len(unverified_affecting)} statutory rule(s) affecting your refund claim are marked **To verify** "
                "because supporting official FTA guidance PDFs have not yet been uploaded. Raseed applies conservative statutory defaults."
            )

    st.markdown("---")

    # Filter / Search
    for r in rules:
        badge = r.get("badge", "🟠 To verify")
        with st.container(border=True):
            c_top1, c_top2 = st.columns([3, 1])
            c_top1.markdown(f"##### {r['id'].replace('RULE_', '').replace('_', ' ').title()}")
            c_top2.markdown(f"**{badge}**")

            st.write(r["description"])
            st.caption(f"📜 **Official Source:** `{r['official_source']}` | **In Force:** {r['effective_from']} to {r['effective_to']}")

            if r.get("matching_citation"):
                st.markdown(f"🔍 *Official Citation: {r['matching_citation']}*")


def render_vat_credit_panel(
    business_id: int,
    current_view: str = "all_periods",
    focus: Optional[str] = None,
    db_path: Optional[Path] = None,
    simple_mode: bool = False,
) -> str:
    """Main renderer for the fixed 50% left visual panel."""
    st.markdown("### 🏛️ VAT Credit Panel")

    ledger = get_credit_ledger(business_id=business_id, db_path=db_path)
    ref = get_refund_status(business_id=business_id, db_path=db_path)
    total_avail = float(ref.get("total_available_credits_aed", 0.0))
    at_risk_amt = float(ref.get("at_risk_credits_aed", 0.0))

    # The headline numbers are shown once, in the cards at the top of the page.

    # View Buttons
    if simple_mode:
        btn_labels = [
            ("Your VAT credits", "all_periods", "View all tax periods and excess VAT balances"),
            ("Expiring soon", "at_risk", "Credits approaching the 5-year statutory deadline"),
            ("Documents", "documents", "Document checklist and uploaded files"),
        ]
        if current_view not in ("all_periods", "at_risk", "documents"):
            current_view = "all_periods"
        v_cols = st.columns(3)
    else:
        btn_labels = [
            ("Your VAT credits", "all_periods", "View all tax periods and excess VAT balances"),
            ("Expiring soon", "at_risk", "Credits approaching the 5-year statutory deadline"),
            ("Raseed's plan", "recovery_plan", "Step-by-step recovery opportunities ordered by urgency"),
            ("Do the numbers match?", "reconciliation", "4-way check between invoices, accounting books, and returns"),
            ("Documents", "documents", "Document checklist and uploaded files"),
            ("Inbox", "inbox", "Raseed's autonomous task inbox"),
            ("Rules", "rules", "Official statutory UAE VAT rules and FTA verification status"),
        ]
        v_cols = st.columns(7)

    selected_view = current_view
    for idx, (label, v_key, tip) in enumerate(btn_labels):
        is_active = current_view == v_key
        if v_cols[idx].button(f"{'▶ ' if is_active else ''}{label}", key=f"panel_btn_{v_key}", use_container_width=True, help=tip):
            selected_view = v_key
            st.session_state.panel_view = v_key
            st.rerun()

    # Dynamic Caption
    if selected_view == "all_periods":
        if focus:
            f_row = next((r for r in ledger if r["period_code"] == focus), None)
            if f_row:
                caption_text = f"Showing: {focus} — AED {float(f_row['net_excess_aed']):,.2f} — expires in {f_row['days_remaining']} days"
            else:
                caption_text = f"Showing: {focus} — Focus details"
        else:
            caption_text = f"Showing: Your VAT credits — {len(ledger)} tax periods analyzed (AED {total_avail:,.2f} money you can get back)"

    elif selected_view == "at_risk":
        at_risk_cnt = sum(1 for r in ledger if r.get("status") in ("URGENT", "DUE_SOON"))
        caption_text = f"Showing: Expiring soon — {at_risk_cnt} credit period(s) you will lose if not claimed before the 5-year deadline"

    elif selected_view == "recovery_plan":
        caption_text = "Showing: Raseed's plan — Prioritized opportunities to claim back your money"

    elif selected_view == "reconciliation":
        caption_text = "Showing: Do the numbers match? — Checking invoices vs books vs returns"

    elif selected_view == "documents":
        ob = onboarding_status(business_id=business_id, db_path=db_path)
        caption_text = f"Showing: Documents — {len(ob.get('present', []))}/7 records verified"

    elif selected_view == "inbox":
        caption_text = "Showing: Inbox — Raseed's autonomous tasks and background monitor items"

    elif selected_view == "rules":
        caption_text = "Showing: Official Tax Rules — Statutory UAE VAT rules and FTA verification status"
    else:
        caption_text = f"Showing: {selected_view}"

    st.caption(f"📌 *{caption_text}*")
    st.markdown("---")

    # Render Active View
    if selected_view == "all_periods":
        render_all_periods_view(business_id=business_id, focus=focus, db_path=db_path, simple_mode=simple_mode)
    elif selected_view == "at_risk":
        render_at_risk_view(business_id=business_id, db_path=db_path)
    elif selected_view == "recovery_plan":
        render_recovery_plan_view(business_id=business_id, db_path=db_path)
    elif selected_view == "reconciliation":
        render_reconciliation_view(business_id=business_id, db_path=db_path)
    elif selected_view == "documents":
        render_documents_view(business_id=business_id, db_path=db_path)
    elif selected_view == "inbox":
        render_inbox_view(business_id=business_id, db_path=db_path)
    elif selected_view == "rules":
        render_rules_view(business_id=business_id, db_path=db_path)

    # Tooltip Tax Glossary at bottom of panel
    with st.expander("📖 Plain English Tax Glossary (?)", expanded=False):
        st.markdown(
            "• **VAT Credit:** Money the UAE Federal Tax Authority (FTA) owes you because you paid more VAT on business purchases than you collected from customers.\n"
            "• **TRN (Tax Registration Number):** Your 15-digit tax ID issued by the FTA, starting with `100`.\n"
            "• **Input VAT:** The 5% VAT you paid to suppliers on business purchases that you can claim back.\n"
            "• **Output VAT:** The VAT you collected from customers on your sales.\n"
            "• **Zero-Rated:** Supplies taxed at 0% (e.g. exports) where you can claim back all input VAT you paid.\n"
            "• **Exempt Supplies:** Supplies not subject to VAT (e.g. residential rent) where input VAT cannot be claimed back.\n"
            "• **Reverse Charge:** Mechanism for imports where the UAE buyer accounts for VAT instead of the overseas supplier.\n"
            "• **5-Year Rule:** Under UAE Tax Law, you must claim excess input VAT within 5 years, after which unclaimed credits are permanently forfeited."
        )

    return selected_view
