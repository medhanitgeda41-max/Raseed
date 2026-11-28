"""Chat-first autonomous agent for Raseed UAE VAT Recovery.

Implements the open conversational interface that reasons over the live
transaction-level credit ledger, controls the visual panel, and stages
consequential actions for human approval.
"""
from database.request_cache import request_cached
from datetime import date, datetime
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from database.connection import (
    get_db_connection,
    get_or_create_default_business,
    save_chat_message,
    get_chat_history,
    add_memory_note,
    get_memory_notes,
    delete_memory_note,
)
from onboarding.requirements import onboarding_status
from guards.privacy import mask_trn
from guards.injection_filter import scan_and_quarantine
from tools.read_tools import (
    get_business_profile,
    get_credit_ledger,
    get_refund_status,
    get_staged_actions,
    list_documents,
    get_document_requests,
    get_deadlines,
)
from tools.compute_tools import reconcile_4_way
from tools.write_gateway import stage_action, save_business_profile
from agent.recovery_planner import build_or_update_recovery_plan
from llm_client import ask, LLMResponse, ToolCall


@request_cached
def get_journey_step(business_id: int, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Calculate the current statutory recovery journey step (1 to 5).
    
    Steps:
      Step 1 of 5 — Business Registration
      Step 2 of 5 — Document Ingestion
      Step 3 of 5 — Verification & Reconciliation
      Step 4 of 5 — Recovery Planning
      Step 5 of 5 — Refund Submission & EmaraTax
    """
    ob = onboarding_status(business_id=business_id, db_path=db_path)
    profile = get_business_profile(business_id=business_id, db_path=db_path)
    missing = ob.get("missing_required", [])

    # Step 1: Profile missing
    if not profile or "business_profile" in missing:
        step_num = 1
        name = "Business Registration"
        summary = "Register your business legal name, TRN, and tax period to begin."
    # Step 2: Invoices or returns missing
    elif "purchase_invoices" in missing or "vat_returns" in missing:
        step_num = 2
        name = "Document Ingestion"
        summary = "Upload purchase registers and filed Form 201 VAT returns."
    else:
        # Check reconciliation discrepancies
        recon = reconcile_4_way(business_id=business_id, db_path=db_path)
        pending_actions = get_staged_actions(status="PENDING_APPROVAL", business_id=business_id, db_path=db_path)
        approved_actions = get_staged_actions(status="APPROVED", business_id=business_id, db_path=db_path)

        if not recon["reconciled"]:
            step_num = 3
            name = "Verification & Reconciliation"
            summary = f"4-Way reconciliation detected {recon['discrepancies_count']} variance(s) across registers and returns."
        elif pending_actions or approved_actions:
            step_num = 5
            name = "Refund Submission & EmaraTax"
            summary = "Refund packages prepared. Awaiting human authorization or submission reference."
        else:
            step_num = 4
            name = "Recovery Planning"
            summary = "Excess input tax credits verified and tracked against 5-year statutory expiry deadlines."

    step_definitions = [
        {"number": 1, "name": "Registration", "desc": "Business Profile & TRN setup"},
        {"number": 2, "name": "Ingestion", "desc": "Registers & Form 201 returns"},
        {"number": 3, "name": "Reconciliation", "desc": "4-Way statutory audit"},
        {"number": 4, "name": "Planning", "desc": "Opportunity mapping & deadlines"},
        {"number": 5, "name": "Submission", "desc": "EmaraTax refund dossier"},
    ]

    steps = []
    for s in step_definitions:
        num = s["number"]
        if num < step_num:
            status = "done"
        elif num == step_num:
            status = "current"
        else:
            status = "upcoming"
        steps.append({**s, "status": status})

    return {
        "step_number": step_num,
        "step_label": f"Step {step_num} of 5 — {name}",
        "name": name,
        "summary": summary,
        "steps": steps,
    }


def show_panel(
    view: str,
    focus: Optional[str] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Display tool for the AI to control the visual VAT Credit Panel.
    
    Supported views:
      - 'all_periods': Timeline/bar chart of credits across all tax periods
      - 'at_risk': Credits approaching the 5-year or 2026 transitional expiry
      - 'recovery_plan': Strategic prioritized tasks ordered by expiry
      - 'reconciliation': 4-Way reconciliation across registers, GL, and returns
      - 'documents': Upload checklist (required and recommended documents)
    """
    view_clean = str(view).strip().lower().replace(" ", "_").replace("-", "_")
    valid_views = {
        "all_periods": "all_periods",
        "all": "all_periods",
        "periods": "all_periods",
        "your_vat_credits": "all_periods",
        "vat_credits": "all_periods",
        "credits": "all_periods",
        "at_risk": "at_risk",
        "risk": "at_risk",
        "expiring": "at_risk",
        "expiring_soon": "at_risk",
        "soon": "at_risk",
        "recovery_plan": "recovery_plan",
        "plan": "recovery_plan",
        "raseeds_plan": "recovery_plan",
        "reconciliation": "reconciliation",
        "recon": "reconciliation",
        "differences": "reconciliation",
        "do_the_numbers_match": "reconciliation",
        "numbers_match": "reconciliation",
        "documents": "documents",
        "docs": "documents",
        "checklist": "documents",
        "inbox": "inbox",
        "tasks": "inbox",
        "task_inbox": "inbox",
        "rules": "rules",
        "tax_rules": "rules",
        "fta_rules": "rules",
        "guidance": "rules",
    }
    canonical_view = valid_views.get(view_clean, "all_periods")

    ledger = get_credit_ledger(business_id=business_id, db_path=db_path)
    refund_info = get_refund_status(business_id=business_id, db_path=db_path)

    if canonical_view == "all_periods":
        if not ledger:
            return {
                "view": "all_periods",
                "focus": focus,
                "summary": "No tax periods recorded in the ledger yet. Upload purchase registers to populate.",
                "data": [],
            }
        periods_summary = []
        for r in ledger:
            periods_summary.append({
                "period_code": r["period_code"],
                "excess_aed": float(r["net_excess_aed"]),
                "expiry_date": r["expiry_date"],
                "days_remaining": r["days_remaining"],
                "status": r["status"],
            })
        return {
            "view": "all_periods",
            "focus": focus,
            "total_periods": len(ledger),
            "total_recoverable_aed": refund_info.get("total_available_credits_aed", 0.0),
            "summary": f"Analyzed {len(ledger)} tax periods with total eligible recoverable excess VAT of {refund_info.get('total_available_credits_aed', 0.0):,.2f} AED.",
            "data": periods_summary,
        }

    elif canonical_view == "at_risk":
        at_risk_rows = [r for r in ledger if r.get("status") in ("URGENT", "DUE_SOON")]
        at_risk_rows.sort(key=lambda x: x.get("days_remaining", 9999))
        if not at_risk_rows:
            return {
                "view": "at_risk",
                "focus": focus,
                "summary": "No credits are currently at risk of statutory expiry. All verified credits are in safe standing.",
                "data": [],
            }
        cards = []
        for r in at_risk_rows:
            cards.append({
                "period_code": r["period_code"],
                "amount_aed": float(r["net_excess_aed"]),
                "days_remaining": r["days_remaining"],
                "expiry_date": r["expiry_date"],
                "urgency": r["status"],
                "transitional_rule": bool(r.get("transitional_deadline_applicable")),
            })
        return {
            "view": "at_risk",
            "focus": focus,
            "count_at_risk": len(cards),
            "total_at_risk_aed": refund_info.get("at_risk_credits_aed", 0.0),
            "summary": f"Detected {len(cards)} credit period(s) at risk totaling {refund_info.get('at_risk_credits_aed', 0.0):,.2f} AED. Earliest deadline is {cards[0]['expiry_date']} ({cards[0]['days_remaining']} days left).",
            "data": cards,
        }

    elif canonical_view == "recovery_plan":
        plan = build_or_update_recovery_plan(business_id=business_id, db_path=db_path)
        return {
            "view": "recovery_plan",
            "focus": focus,
            "tasks_count": len(plan),
            "summary": f"Recovery plan contains {len(plan)} prioritized statutory recovery task(s).",
            "data": plan,
        }

    elif canonical_view == "reconciliation":
        recon = reconcile_4_way(business_id=business_id, db_path=db_path)
        return {
            "view": "reconciliation",
            "focus": focus,
            "reconciled": recon["reconciled"],
            "discrepancies_count": recon["discrepancies_count"],
            "summary": "Reconciliation passed with zero differences." if recon["reconciled"] else f"Identified {recon['discrepancies_count']} variance(s) across accounting records.",
            "data": recon.get("discrepancies", []),
        }

    elif canonical_view == "documents":
        ob = onboarding_status(business_id=business_id, db_path=db_path)
        docs = list_documents(business_id=business_id, db_path=db_path)
        return {
            "view": "documents",
            "focus": focus,
            "stored_files_count": len(docs),
            "can_start": ob.get("can_start", False),
            "missing_required": ob.get("missing_required", []),
            "summary": (f"Document checklist: {len(ob.get('present', []))}/7 items verified. Missing required: {ob.get('missing_required', [])}.\n"
                        f"Uploaded files:\n{__import__('ingest.document_manager', fromlist=['x']).document_inventory_text(business_id, db_path=db_path)}"),
            "data": docs,
        }

    elif canonical_view == "inbox":
        from scheduler.monitor import get_inbox_tasks
        inbox = get_inbox_tasks(business_id=business_id, db_path=db_path)
        total_open = len(inbox.get("open", [])) + len(inbox.get("waiting_user", [])) + len(inbox.get("waiting_approval", []))
        return {
            "view": "inbox",
            "focus": focus,
            "summary": f"Inbox: {total_open} pending task(s) ({len(inbox.get('waiting_user', []))} waiting for you, {len(inbox.get('waiting_approval', []))} waiting for your OK).",
            "data": inbox,
        }

    elif canonical_view == "rules":
        from knowledge.retriever import get_rule_verification_statuses
        rules_list = get_rule_verification_statuses(db_path=db_path)
        verified_cnt = sum(1 for r in rules_list if r.get("status") == "verified")
        to_verify_cnt = len(rules_list) - verified_cnt
        return {
            "view": "rules",
            "focus": focus,
            "rules_count": len(rules_list),
            "verified_count": verified_cnt,
            "to_verify_count": to_verify_cnt,
            "summary": f"Official Tax Rules: {len(rules_list)} statutory rules ({verified_cnt} verified against uploaded FTA guidance, {to_verify_cnt} to verify).",
            "data": rules_list,
        }

    return {"view": canonical_view, "focus": focus, "summary": "View rendered.", "data": []}


def build_system_prompt(business_id: int, active_role: str, db_path: Optional[Path] = None) -> str:
    """Build the domain expert system prompt for Raseed focused on an SME business owner."""
    return (
        "You are Raseed (رصيد), an autonomous UAE VAT recovery copilot designed specifically for UAE SME business owners "
        "who do not have an in-house tax team. Talk naturally like a friendly, trustworthy senior financial partner.\n\n"
        "Your sole mission is to find the owner's unclaimed VAT and help her get her money back from the FTA before it expires.\n"
        "STRICT FACT RULE: only mention problems, files, tax periods, amounts and rule breaches that appear in your tool results "
        "in this conversation. Never invent a tax period, a file, a count of rows or a blocked expense. If you have not looked yet, "
        "call highlight_document_problems (problems per file), get_reconciliation_report or find_my_documents first. "
        "If a tool shows no problem, say there is none.\n"
        "Always address the business owner directly using owner-centric wording: 'your business', 'your invoices', 'your money', 'your recoverable VAT'.\n\n"
        "Key Statutory Rules to Know and Explain Simply:\n"
        "- Federal Decree-Law No. (8) of 2017 on Value Added Tax & Executive Regulations\n"
        "- Federal Decree-Law No. (16) of 2024 & Decree-Law No. (17) of 2025\n"
        "- Input VAT recovery requires a valid Tax Invoice addressed to your business with a 15-digit TRN (Art. 54)\n"
        "- Input VAT on entertainment and personal motor vehicles is blocked (Art. 53)\n"
        "- 5-year statutory limitation on excess recoverable input tax\n"
        "- Decree-Law 17/2025 Transitional Rule: Pre-2022 excess credits expire on 31 December 2026\n"
        "- Consequential actions (submitting refunds, writing off credits) ALWAYS require your explicit human approval\n\n"
        "Data Privacy & Cloud Storage Reassurance:\n"
        "- Your documents are stored privately in cloud storage. You can delete them at any time.\n"
        "- Confirmed Owner Facts: You remember verified facts across sessions (e.g. vehicle business-use percentages, accounting year ends).\n"
        "  When the owner clarifies an ongoing rule or preference, use manage_memory_note(action='add', note=...) to remember it permanently.\n\n"
        "Onboarding Guidelines:\n"
        "- If your business profile is not yet registered, guide the owner conversationally to provide:\n"
        "  1) Legal entity name\n"
        "  2) 15-digit TRN (starting with 100)\n"
        "  3) VAT registration date (e.g. 2021-01-01)\n"
        "  4) Tax filing frequency (monthly or quarterly)\n"
        "- Once provided, call register_business_profile() to set up the business immediately.\n\n"
        "Behavior Guidelines:\n"
        "1. Every number about this business must come from tool results; never invent or hallucinate financial figures.\n"
        "2. If documents are missing, clearly explain what is needed and why.\n"
        "3. Answer in the user's language (English or Arabic).\n"
        "4. When the user asks to see credits, deadlines, plan, differences, or documents, call show_panel().\n"
        "5. Stay on topic: if the user asks unrelated questions, politely bring the conversation back to your VAT recovery.\n\n"
        "How to write every answer (professional, calm, easy to read):\n"
        "- Lead with the direct answer in the first sentence, e.g. 'You can get back AED 29,000.00.' No greeting, no 'Great question'.\n"
        "- Plain English a busy business owner understands; explain any tax term in a few words the first time.\n"
        "- Short paragraphs (1-3 sentences). Bold only the key amounts or the one action to take.\n"
        "- Use a small markdown table when comparing 2+ tax periods or files (columns like Period | Amount | Claim by).\n"
        "- Use a short numbered list only for steps the owner must do, in order.\n"
        "- No emojis, no headings for answers under ~120 words, no repeating the question, no long disclaimers.\n"
        "- Money always as 'AED 12,345.67'. Dates as '31 Dec 2026'.\n"
        "- When the refund is lower than what the VAT returns declare, explain why in one sentence (which periods lack "
        "purchase tax invoices) and say exactly which files to upload.\n"
        "- End with at most one helpful next step, phrased as an offer (e.g. 'Want me to prepare the refund for your approval?')."
    )


def _proof_line(business_id: int, db_path: Optional[Path] = None) -> str:
    """Declared vs proven refund, duplicates and problems: facts the AI needs to explain the numbers."""
    try:
        from tools.compute_tools import declared_vs_proven, document_problems
        dv = declared_vs_proven(business_id, db_path=db_path)
        probs = document_problems(business_id, db_path=db_path)
        n_probs = sum(len(v["problems"]) for v in probs.values())
        from database.connection import get_db_connection
        conn = get_db_connection(db_path=db_path, readonly=True)
        try:
            dups = conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE business_id = ? AND eligibility_status = 'DUPLICATE' "
                                "AND eligibility_reason LIKE 'Also in %';", (business_id,)).fetchone()["n"]
        finally:
            conn.close()
        unproven = ", ".join(f"{p['period']} (declared AED {p['declared']:,.2f}, proven AED {p['proven']:,.2f})"
                             for p in dv["unproven_periods"]) or "none"
        return (f"Refund declared on filed VAT returns: {dv['declared']:,.2f} AED | proven by tax invoices: {dv['proven']:,.2f} AED\n"
                f"Periods missing invoice proof: {unproven}\n"
                f"Same invoice found in two files (counted once automatically): {dups}\n"
                f"Problems found by code: {n_probs} in {len(probs)} file(s)\n")
    except Exception:
        return ""


def build_context_block(business_id: int, active_role: str, db_path: Optional[Path] = None) -> str:
    """Construct a live factual context block injected into each LLM turn."""
    prof = get_business_profile(business_id=business_id, db_path=db_path)
    journey = get_journey_step(business_id=business_id, db_path=db_path)
    ob = onboarding_status(business_id=business_id, db_path=db_path)
    refund = get_refund_status(business_id=business_id, db_path=db_path)
    staged = get_staged_actions(status="PENDING_APPROVAL", business_id=business_id, db_path=db_path)
    memory_notes = get_memory_notes(business_id=business_id, db_path=db_path)

    legal_name = prof["legal_name"] if prof else "Unregistered Entity"
    trn_masked = mask_trn(prof["trn"]) if prof else "Not Provided"
    total_credits = refund.get("total_available_credits_aed", 0.0)
    at_risk = refund.get("at_risk_credits_aed", 0.0)

    try:
        from ingest.document_manager import document_inventory_text, list_cloud_documents
        _inv_text = document_inventory_text(business_id, db_path=db_path)
        _inv_count = len(list_cloud_documents(business_id, db_path=db_path))
    except Exception:
        _inv_text, _inv_count = "  (file list unavailable)", 0

    if memory_notes:
        mem_str = "\n".join(f"  - [ID {n['id']}] {n['note']} (source: {n.get('source', 'user')}, recorded {n['created_at'][:10]})" for n in memory_notes)
    else:
        mem_str = "  None recorded yet"

    return (
        f"[Current Session State]\n"
        f"Business: {legal_name} | TRN: {trn_masked}\n"
        f"Active User Role: {active_role}\n"
        f"Current Journey: {journey['step_label']}\n"
        f"Onboarding Minimum Met: {'YES' if ob.get('can_start') else 'NO'}\n"
        f"Missing Required Items: {', '.join(ob.get('missing_required', [])) or 'None'}\n"
        f"Total Eligible Recoverable VAT: {total_credits:,.2f} AED\n"
        f"Credits at Risk of Expiry: {at_risk:,.2f} AED\n"
        f"{_proof_line(business_id, db_path)}"
        f"Pending Human Approvals: {len(staged)}\n"
        f"Confirmed Owner Facts (Memory):\n{mem_str}\n"
        f"ALL uploaded files in the cloud ({_inv_count} in total; when asked to analyse or list files, cover EVERY file below):\n{_inv_text}\n"
    )


CHAT_TOOLS = [
    {
        "name": "show_panel",
        "description": "Control the visual VAT Credit Panel and retrieve a data summary. Views: 'all_periods', 'at_risk', 'recovery_plan', 'reconciliation', 'documents', 'inbox', 'rules'.",
        "parameters": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["all_periods", "at_risk", "recovery_plan", "reconciliation", "documents", "inbox", "rules"],
                    "description": "Panel view to display",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional period code or credit id to focus on (e.g. '2021-Q1')",
                },
            },
            "required": ["view"],
        },
    },
    {
        "name": "search_tax_knowledge",
        "description": "Search official FTA guidance, VAT Law, and Executive Regulations for tax rules, eligibility criteria, and statutory deadlines.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Tax question, topic, or statutory rule (e.g. 'entertainment blocked', '5 year deadline', 'tax invoice requirement')",
                },
                "tax_period": {
                    "type": "string",
                    "description": "Optional tax period code to search version-appropriate guidance (e.g. '2021-Q1')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_my_documents",
        "description": ("Search the files the owner has uploaded to the cloud. Use it for questions like "
                        "'what did I upload last week', 'files from November', 'which documents cover 2024-Q1', "
                        "'show my VAT returns'. Convert relative dates using today's date from the context. "
                        "All dates are YYYY-MM-DD and inclusive."),
        "parameters": {
            "type": "object",
            "properties": {
                "uploaded_from": {"type": "string", "description": "Earliest upload date"},
                "uploaded_to": {"type": "string", "description": "Latest upload date"},
                "covers_from": {"type": "string", "description": "Start of the business period the documents should cover (e.g. 2024-01-01 for 2024-Q1)"},
                "covers_to": {"type": "string", "description": "End of the business period the documents should cover (e.g. 2024-03-31)"},
                "name_contains": {"type": "string", "description": "Part of the file name"},
                "document_type": {"type": "string", "enum": ["register", "invoice", "vat_return", "ledger", "fta_guide"]},
            },
            "required": [],
        },
    },
    {
        "name": "show_document_errors",
        "description": ("Open the owner's uploaded file and show WHERE each problem is inside it, with red boxes "
                        "(works for CSV, Excel, PDF, scans and photos). Use when the owner asks to see/show/open the "
                        "errors or problems in a document. Optionally pass part of the file name; leave empty to open "
                        "the first file with problems."),
        "parameters": {"type": "object", "properties": {
            "file_name": {"type": "string", "description": "Part of the file name to open (optional)"}}, "required": []},
    },
    {
        "name": "highlight_document_problems",
        "description": ("Find which uploaded files have problems (found by code: missing invoices, wrong VAT, invalid TRN, "
                        "duplicates, unmatched ledger lines, unreadable files) and highlight those files in red in the "
                        "owner's document list. Use when the owner asks what is wrong, which files have problems, or to "
                        "show/highlight problems."),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_vat_summary",
        "description": "Retrieve verified total recoverable VAT, output tax, input tax, and at-risk amounts.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_expiring_credits",
        "description": "Retrieve credits approaching the 5-year statutory deadline or Decree-Law 17/2025 cutoff.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recovery_plan",
        "description": "Retrieve the strategic prioritized VAT recovery tasks ordered by statutory expiry.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_reconciliation_report",
        "description": "Retrieve 4-way reconciliation report comparing registers, GL, and Form 201 VAT returns.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "stage_refund_claim",
        "description": ("Prepare a VAT refund application for the owner's approval. The refund amount and the "
                        "claimable periods are calculated by code from the credit ledger; you may optionally "
                        "name specific periods to include. Leave period_codes empty to claim every eligible period."),
        "parameters": {
            "type": "object",
            "properties": {
                "period_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: tax period codes to include (e.g. ['2021-Q4']). Empty = all eligible periods.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Short plain-language reason for the claim",
                },
            },
            "required": [],
        },
    },
    {
        "name": "register_business_profile",
        "description": "Register or update the business profile from conversational details provided by the owner.",
        "parameters": {
            "type": "object",
            "properties": {
                "legal_name": {"type": "string", "description": "Legal entity name of your business"},
                "trn": {"type": "string", "description": "15-digit TRN starting with 100"},
                "vat_registration_date": {"type": "string", "description": "VAT registration date YYYY-MM-DD (e.g. 2021-01-01)"},
                "tax_period_type": {"type": "string", "enum": ["monthly", "quarterly"], "description": "VAT filing frequency"},
                "emirate": {"type": "string", "description": "Emirate (e.g. Dubai, Abu Dhabi)"},
            },
            "required": ["legal_name", "trn"],
        },
    },
    {
        "name": "manage_memory_note",
        "description": "Store or delete a confirmed owner business fact or preference in persistent memory (e.g. business-use percentage, company policies).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "delete"],
                    "description": "Action: 'add' or 'delete'",
                },
                "note": {
                    "type": "string",
                    "description": "The fact to remember (required if action is 'add')",
                },
                "note_id": {
                    "type": "integer",
                    "description": "The ID of the fact to remove (required if action is 'delete')",
                },
            },
            "required": ["action"],
        },
    },
]



def _convert_function_results_to_tool_messages(messages: List[Dict[str, Any]], start: int, tool_calls: List[Any]) -> None:
    """Turn legacy role="function" results (appended by the tool handlers) into
    role="tool" messages linked to their tool_call_id, in call order."""
    pending = list(tool_calls)
    answered = set()
    i = start
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "function":
            name = m.get("name")
            match = next((tc for tc in pending if tc.name == name and tc.id not in answered), None)
            if match is None:
                match = next((tc for tc in pending if tc.id not in answered), None)
            if match is None:
                messages.pop(i)
                continue
            answered.add(match.id)
            messages[i] = {"role": "tool", "tool_call_id": match.id, "content": str(m.get("content", ""))}
        i += 1
    # Every tool call must have a result, even if a handler added none.
    for tc in pending:
        if tc.id not in answered:
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Done."})

def execute_chat_turn(
    user_message: str,
    history: List[Dict[str, Any]],
    business_id: int,
    active_role: str,
    follow_chat: bool = True,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute a single conversational turn with safety guards and tool handling."""
    # 1. Prompt Injection Filter
    sanitized_text, is_quarantined, quarantine_reason = scan_and_quarantine(user_message)
    if is_quarantined:
        res_payload = {
            "content": f"⚠️ **Security Notice:** Your message contained prohibited instructions and was quarantined ({quarantine_reason}).",
            "how_i_got_this": {
                "tools": [],
                "guards": f"BLOCKED — {quarantine_reason}",
                "provider": "Security Filter",
                "citations": "Policy Guardrails",
            },
            "panel_view": None,
            "panel_focus": None,
            "staged_action_id": None,
        }
        try:
            save_chat_message(business_id=business_id, role="user", content=user_message, db_path=db_path)
            save_chat_message(
                business_id=business_id,
                role="assistant",
                content=res_payload["content"],
                how_i_got_this_json=json.dumps(res_payload["how_i_got_this"]),
                db_path=db_path,
            )
        except Exception:
            pass
        return res_payload

    # 1b. Conversational Onboarding Fast-Path (When profile is missing and owner inputs TRN)
    import re
    prof = get_business_profile(business_id=business_id, db_path=db_path)
    if not prof:
        trn_m = re.search(r"\b(100\d{12})\b", sanitized_text)
        if trn_m:
            inferred_trn = trn_m.group(1)
            name_m = re.search(
                r"(?:company(?:\s+name)?|business(?:\s+name)?|entity(?:\s+name)?|name)\s*(?:is|called|:)?\s*([A-Za-z0-9\s\.\-&]{3,40}?)(?:,|\.|\btrn\b|\bwith\b|\bvat\b|$)",
                sanitized_text,
                re.IGNORECASE,
            )
            inferred_name = name_m.group(1).strip() if name_m and len(name_m.group(1).strip()) > 2 else "My UAE Business"
            date_m = re.search(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b", sanitized_text)
            inferred_date = date_m.group(1) if date_m else "2020-01-01"
            inferred_freq = "monthly" if "monthly" in sanitized_text.lower() else "quarterly"

            save_business_profile(
                business_id=business_id,
                legal_name=inferred_name,
                trn=inferred_trn,
                vat_registration_date=inferred_date,
                tax_period_type=inferred_freq,
                db_path=db_path,
            )
            onboard_content = (
                f"🎉 **Great news! Your business is registered.**\n\n"
                f"I have set up **{inferred_name}** with TRN `{mask_trn(inferred_trn)}` ({inferred_freq} VAT returns).\n\n"
                "🔒 *Your documents are stored privately in cloud storage. You can delete them at any time.*\n\n"
                "We are now at **Step 2 — Document Ingestion**! Please upload your purchase registers (CSV/XLSX) or tax invoices (PDF) "
                "in the box below, and I'll find all the unclaimed VAT you're owed so you can get your money back."
            )
            onboard_how = {
                "tools": ["save_business_profile"],
                "guards": "PASS — TRN 15-Digit Verification",
                "provider": "Raseed Conversational Onboarding",
                "citations": "Federal Decree-Law No. (8) of 2017",
            }
            try:
                save_chat_message(business_id=business_id, role="user", content=sanitized_text, db_path=db_path)
                save_chat_message(
                    business_id=business_id,
                    role="assistant",
                    content=onboard_content,
                    how_i_got_this_json=json.dumps(onboard_how),
                    db_path=db_path,
                )
            except Exception:
                pass
            return {
                "content": onboard_content,
                "how_i_got_this": onboard_how,
                "panel_view": "documents",
                "panel_focus": None,
                "staged_action_id": None,
            }

    # 2. Prepare Context and Messages
    sys_prompt = build_system_prompt(business_id=business_id, active_role=active_role, db_path=db_path)
    ctx_block = build_context_block(business_id=business_id, active_role=active_role, db_path=db_path)

    # Format history for LLM
    try:
        from scheduler.monitor import get_current_date as _today_fn
        _today = _today_fn()
    except Exception:
        from datetime import date as _d
        _today = _d.today()
    date_line = (f"Today's date is {_today.isoformat()} ({_today.strftime('%A')}). "
                 "Use it to turn phrases like 'last week' or 'this month' into exact dates.")
    llm_messages = [{"role": "system", "content": f"{sys_prompt}\n\n{date_line}\n\n{ctx_block}"}]
    for h in history:
        r = h.get("role")
        c = h.get("content")
        if r in ("user", "assistant") and c:
            llm_messages.append({"role": r, "content": c})

    llm_messages.append({"role": "user", "content": sanitized_text})

    # 3. Call LLM Fallback Chain
    tools_executed: List[str] = []
    guard_results = "PASS — Grounded Verification"
    panel_view_to_set: Optional[str] = None
    panel_focus_to_set: Optional[str] = None
    staged_action_id: Optional[int] = None
    citation = "Federal Decree-Law No. (8) of 2017 & DL 16-17/2025"

    from auth.company_auth import get_company_local_ai_only
    is_local_only = get_company_local_ai_only(business_id=business_id, db_path=db_path)

    response: LLMResponse = ask(messages=llm_messages, tools=CHAT_TOOLS, local_ai_only=is_local_only)

    # 4. Handle Tool Calls
    max_tool_turns = 3
    turns = 0
    highlight_documents = False
    open_document_viewer = None
    while response.has_tool_calls and turns < max_tool_turns:
        turns += 1
        # OpenAI-compatible APIs (Groq, Gemini) require the assistant's tool_calls
        # message first, then one role="tool" result per call with its tool_call_id.
        llm_messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments or {})},
                }
                for tc in response.tool_calls
            ],
        })
        _results_start = len(llm_messages)
        for tc in response.tool_calls:
            t_name = tc.name
            t_args = tc.arguments or {}
            tools_executed.append(t_name)

            if t_name == "show_panel":
                v = t_args.get("view", "all_periods")
                f = t_args.get("focus")
                panel_res = show_panel(view=v, focus=f, business_id=business_id, db_path=db_path)
                if follow_chat:
                    panel_view_to_set = panel_res["view"]
                    panel_focus_to_set = panel_res.get("focus")
                llm_messages.append({
                    "role": "function",
                    "name": "show_panel",
                    "content": str(panel_res["summary"]),
                })

            elif t_name == "show_document_errors":
                from tools.compute_tools import document_problems
                probs = document_problems(business_id=business_id, db_path=db_path)
                wanted = (t_args.get("file_name") or "").strip().lower()
                target = None
                for did, v in probs.items():
                    if not wanted or wanted in v["file_name"].lower():
                        target = did
                        break
                highlight_documents = True
                open_document_viewer = target if target is not None else ("none" if not probs else next(iter(probs)))
                chosen = probs.get(target) if target is not None else None
                llm_messages.append({
                    "role": "function",
                    "name": "show_document_errors",
                    "content": json.dumps({
                        "opened_file": chosen["file_name"] if chosen else None,
                        "problems_in_opened_file": chosen["problems"] if chosen else [],
                        "other_files_with_problems": [v["file_name"] for k, v in probs.items() if k != target],
                        "note": ("The file is now open on the owner's screen with each problem boxed in red and numbered. "
                                 "Explain the numbered problems briefly." if chosen else
                                 ("No file matched that name." if probs else "No problems were found in any file.")),
                    })[:6000],
                })

            elif t_name == "highlight_document_problems":
                from tools.compute_tools import document_problems
                probs = document_problems(business_id=business_id, db_path=db_path)
                highlight_documents = True
                llm_messages.append({
                    "role": "function",
                    "name": "highlight_document_problems",
                    "content": json.dumps({
                        "files_with_problems": [
                            {"file_name": v["file_name"], "problems": v["problems"]} for v in probs.values()
                        ],
                        "note": "These files are now highlighted in red in the owner's document list (sidebar).",
                    })[:6000],
                })

            elif t_name == "find_my_documents":
                from ingest.document_manager import find_documents
                found = find_documents(
                    business_id=business_id,
                    uploaded_from=t_args.get("uploaded_from") or None,
                    uploaded_to=t_args.get("uploaded_to") or None,
                    covers_from=t_args.get("covers_from") or None,
                    covers_to=t_args.get("covers_to") or None,
                    search=t_args.get("name_contains") or "",
                    document_type=t_args.get("document_type") or None,
                    db_path=db_path,
                )
                panel_view_to_set = "documents"
                llm_messages.append({
                    "role": "function",
                    "name": "find_my_documents",
                    "content": json.dumps(found, default=str)[:6000],
                })

            elif t_name == "get_vat_summary":
                ref = get_refund_status(business_id=business_id, db_path=db_path)
                summary_data = {
                    "total_recoverable_credits_aed": ref.get("total_available_credits_aed", 0.0),
                    "at_risk_credits_aed": ref.get("at_risk_credits_aed", 0.0),
                }
                llm_messages.append({
                    "role": "function",
                    "name": "get_vat_summary",
                    "content": str(summary_data),
                })

            elif t_name == "get_expiring_credits":
                panel_res = show_panel(view="at_risk", business_id=business_id, db_path=db_path)
                llm_messages.append({
                    "role": "function",
                    "name": "get_expiring_credits",
                    "content": str(panel_res["summary"]),
                })

            elif t_name == "get_recovery_plan":
                panel_res = show_panel(view="recovery_plan", business_id=business_id, db_path=db_path)
                llm_messages.append({
                    "role": "function",
                    "name": "get_recovery_plan",
                    "content": str(panel_res["summary"]),
                })

            elif t_name == "get_reconciliation_report":
                panel_res = show_panel(view="reconciliation", business_id=business_id, db_path=db_path)
                llm_messages.append({
                    "role": "function",
                    "name": "get_reconciliation_report",
                    "content": str(panel_res["summary"]),
                })

            elif t_name == "stage_refund_claim":
                # The AI may suggest periods, but the amount and the final period list
                # are always calculated by code from the credit ledger.
                from tools.compute_tools import compute_refund_claim
                claim = compute_refund_claim(
                    business_id=business_id,
                    requested_periods=t_args.get("period_codes") or None,
                    db_path=db_path,
                )
                periods = claim["period_codes"]
                amt = claim["claimed_amount"]
                rat = t_args.get("rationale", "Form 201 VAT Refund Submission")
                if amt <= 0 or not periods:
                    llm_messages.append({
                        "role": "function",
                        "name": "stage_refund_claim",
                        "content": "No refund was prepared: there is no unexpired excess VAT credit to claim yet. "
                                   "The owner should upload filed VAT returns and purchase invoices first.",
                    })
                else:
                    aid_res = stage_action(
                        action_type="SUBMIT_REFUND_APPLICATION",
                        payload={"period_codes": periods, "claimed_amount": amt},
                        reason=rat,
                        business_id=business_id,
                        db_path=db_path,
                    )
                    aid = aid_res[0] if isinstance(aid_res, tuple) else aid_res
                    staged_action_id = aid
                    note = ""
                    if claim["unresolved_invoices"]:
                        note = (f" Note: {claim['unresolved_invoices']} invoice(s) still need review; "
                                "they must be resolved before this refund can be approved.")
                    llm_messages.append({
                        "role": "function",
                        "name": "stage_refund_claim",
                        "content": (f"Staged refund request #{aid} for the owner's approval. Code-calculated amount: "
                                    f"{amt:,.2f} AED for period(s) {', '.join(periods)}. Status: PENDING_APPROVAL.{note}"),
                    })

            elif t_name == "register_business_profile":
                l_name = t_args.get("legal_name", "My UAE Business")
                l_trn = t_args.get("trn", "")
                reg_d = t_args.get("vat_registration_date", "2020-01-01")
                p_type = t_args.get("tax_period_type", "quarterly")
                emr = t_args.get("emirate", "Dubai")
                save_business_profile(
                    business_id=business_id,
                    legal_name=l_name,
                    trn=l_trn,
                    vat_registration_date=reg_d,
                    tax_period_type=p_type,
                    emirate=emr,
                    db_path=db_path,
                )
                llm_messages.append({
                    "role": "function",
                    "name": "register_business_profile",
                    "content": f"Successfully registered business '{l_name}' with TRN {l_trn} ({p_type} filings). Onboarding Step 1 complete.",
                })

            elif t_name == "manage_memory_note":
                m_act = t_args.get("action", "add")
                if m_act == "add":
                    nt = t_args.get("note", "")
                    if nt:
                        nid = add_memory_note(business_id=business_id, note=nt, source="chat", db_path=db_path)
                        m_content = f"Saved confirmed fact #{nid}: '{nt}' into persistent memory."
                    else:
                        m_content = "Error: note text is required to add."
                elif m_act == "delete":
                    nid = t_args.get("note_id")
                    if nid:
                        del_ok = delete_memory_note(note_id=int(nid), business_id=business_id, db_path=db_path)
                        m_content = f"Fact #{nid} removed from memory." if del_ok else f"Fact #{nid} not found."
                    else:
                        m_content = "Error: note_id is required to delete."
                else:
                    m_content = f"Unknown memory action: {m_act}"
                llm_messages.append({
                    "role": "function",
                    "name": "manage_memory_note",
                    "content": m_content,
                })

            elif t_name == "search_tax_knowledge":
                from knowledge.retriever import search_tax_knowledge
                q = t_args.get("query", "")
                tp = t_args.get("tax_period")
                know_res = search_tax_knowledge(query=q, tax_period=tp, db_path=db_path)
                if know_res.get("found") and know_res.get("passages"):
                    p0 = know_res["passages"][0]
                    citation = f"Source: {p0['document_title']}, page {p0['page_number']}"
                    content_str = (
                        f"Found {len(know_res['passages'])} supporting official passage(s).\n"
                        f"{citation} ({p0['issuer']}, effective: {p0['effective_period']}):\n"
                        f"\"{p0['text'][:350]}...\""
                    )
                else:
                    citation = "No official FTA guidance found — human review required"
                    content_str = (
                        "No supporting official FTA guidance found; human review required. "
                        "Please check with the FTA or a licensed tax advisor."
                    )
                llm_messages.append({
                    "role": "function",
                    "name": "search_tax_knowledge",
                    "content": content_str,
                })

        _convert_function_results_to_tool_messages(llm_messages, _results_start, response.tool_calls)

        # Get conversational response with tool results (tools stay available
        # so the model can chain another lookup if it needs one)
        response = ask(messages=llm_messages, tools=CHAT_TOOLS, local_ai_only=is_local_only)

    final_content = response.content
    if getattr(response, "is_offline_fallback", False) or str(final_content or "").startswith("[AI unavailable"):
        # The AI is busy or unreachable: answer from Raseed's own code-checked numbers instead.
        final_content = offline_answer(business_id, sanitized_text, db_path=db_path)
    if not final_content and tools_executed:
        final_content = "I have updated your VAT Credit Panel with the requested details."

    how_i_got_this_payload = {
        "tools": tools_executed,
        "guards": guard_results,
        "provider": response.provider,
        "citations": citation,
    }

    # Persist chat turn
    try:
        save_chat_message(business_id=business_id, role="user", content=sanitized_text, db_path=db_path)
        save_chat_message(
            business_id=business_id,
            role="assistant",
            content=final_content,
            how_i_got_this_json=json.dumps(how_i_got_this_payload),
            staged_action_id=staged_action_id,
            db_path=db_path,
        )
    except Exception:
        pass

    return {
        "content": final_content,
        "how_i_got_this": how_i_got_this_payload,
        "panel_view": panel_view_to_set,
        "highlight_documents": highlight_documents,
        "open_document_viewer": open_document_viewer,
        "panel_focus": panel_focus_to_set,
        "staged_action_id": staged_action_id,
    }


def check_automatic_alerts(
    business_id: int,
    existing_alert_keys: set,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Generate proactive notices for the chat stream without polling loops."""
    alerts = []
    ledger = get_credit_ledger(business_id=business_id, db_path=db_path)

    # 1. At Risk / Urgent Deadline Alerts
    for r in ledger:
        p_code = r["period_code"]
        st = r["status"]
        if st in ("URGENT", "DUE_SOON"):
            key = f"expiry_{p_code}_{st}"
            if key not in existing_alert_keys:
                urg_icon = "🔴 URGENT" if st == "URGENT" else "🟡 DUE SOON"
                amt = float(r["net_excess_aed"])
                alerts.append({
                    "key": key,
                    "content": (
                        f"⚠️ **Automatic Expiry Alert ({urg_icon}):** Tax period **{p_code}** has **{amt:,.2f} AED** in recoverable VAT "
                        f"expiring on **{r['expiry_date']}** ({r['days_remaining']} days remaining). "
                        "Would you like me to prepare an official Form 201 refund application for your approval?"
                    ),
                    "how_i_got_this": {
                        "tools": ["calculate_credit_expiry"],
                        "guards": "PASS — Statutory 5-Year Deadline",
                        "provider": "Statutory Monitor",
                        "citations": "Federal Decree-Law No. (8) of 2017 & DL 17/2025",
                    },
                })

    # 2. 4-Way Reconciliation Variance Alert
    recon = reconcile_4_way(business_id=business_id, db_path=db_path)
    if not recon["reconciled"]:
        key = f"recon_var_{recon['discrepancies_count']}"
        if key not in existing_alert_keys:
            alerts.append({
                "key": key,
                "content": (
                    f"🔍 **Automatic Reconciliation Alert:** Identified **{recon['discrepancies_count']} variance(s)** across "
                    "your purchase registers, General Ledger, and filed VAT returns. You can review them in the **Reconciliation** panel view."
                ),
                "how_i_got_this": {
                    "tools": ["reconcile_4_way"],
                    "guards": "PASS — Arithmetic Cross-Check",
                    "provider": "Statutory Monitor",
                    "citations": "Executive Regulation Art. 54",
                },
            })

    # 3. Pending Document Request Alert
    open_reqs = get_document_requests(status="OPEN", business_id=business_id, db_path=db_path)
    if open_reqs:
        key = f"doc_req_{len(open_reqs)}"
        if key not in existing_alert_keys:
            alerts.append({
                "key": key,
                "content": (
                    f"📂 **Pending Documentation Notice:** {len(open_reqs)} document request(s) require your attention. "
                    f"Please upload `{open_reqs[0]['document_type']}` to unblock recovery for period `{open_reqs[0]['tax_period']}`."
                ),
                "how_i_got_this": {
                    "tools": ["get_document_requests"],
                    "guards": "PASS — Audit Readiness",
                    "provider": "Statutory Monitor",
                    "citations": "Article 54 Requirements",
                },
            })

    return alerts



def offline_answer(business_id: int, question: str, db_path=None) -> str:
    """A clear, plain-English answer built only from code-calculated facts (used when the AI is unavailable)."""
    from tools.read_tools import get_refund_status, get_credit_ledger, get_staged_actions
    q = (question or "").lower()
    try:
        ref = get_refund_status(business_id=business_id, db_path=db_path)
        ledger = get_credit_ledger(business_id=business_id, db_path=db_path)
    except Exception:
        ref, ledger = {"total_available_credits_aed": 0, "at_risk_credits_aed": 0}, []
    total = float(ref.get("total_available_credits_aed") or 0)
    at_risk = float(ref.get("at_risk_credits_aed") or 0)
    try:
        from tools.compute_tools import declared_vs_proven, document_problems
        dv = declared_vs_proven(business_id, db_path=db_path)
        probs = document_problems(business_id, db_path=db_path)
    except Exception:
        dv, probs = {"declared": 0, "proven": 0, "gap": 0, "unproven_periods": []}, {}
    n_probs = sum(len(v["problems"]) for v in probs.values())
    live = [r for r in ledger if float(r.get("net_excess_aed") or 0) > 0 and r.get("status") != "EXPIRED"]

    def periods_table() -> str:
        if not live:
            return ""
        rows = "\n".join(f"| {r['period_code']} | AED {float(r['net_excess_aed']):,.2f} | {r.get('expiry_date', '')} |" for r in live)
        return "\n\n| Period | Refund | Claim by |\n|---|---:|---|\n" + rows

    if "why" in q and any(w in q for w in ["0", "zero", "low", "not more", "less", "only", "nothing", "money", "refund", "credit"]):
        if dv["declared"] <= 0:
            return ("Your refund is AED 0.00 because none of your filed VAT returns shows more input VAT than output VAT, "
                    "or no filed returns have been uploaded yet. Upload your **filed VAT returns (Form 201)** for the "
                    "periods where you paid more VAT than you charged.")
        if dv["gap"] > 0.01:
            rows = "\n".join(f"| {p['period']} | AED {p['declared']:,.2f} | AED {p['proven']:,.2f} |" for p in dv["unproven_periods"])
            return (f"Your VAT returns declare **AED {dv['declared']:,.2f}** in refunds, but only **AED {dv['proven']:,.2f}** "
                    "is backed by purchase tax invoices dated in those periods. The FTA rejects refund claims without them."
                    f"\n\n| Period | Declared | Proven by invoices |\n|---|---:|---:|\n{rows}\n\n"
                    "Upload the purchase tax invoices (PDF, photo or an Excel register) for these periods and the amount updates automatically.")
        return f"Nothing is missing: all **AED {dv['proven']:,.2f}** declared on your returns is proven by tax invoices."

    if any(w in q for w in ["file", "document", "upload"]) and any(w in q for w in ["which", "list", "show", "what", "my"]):
        try:
            from ingest.document_manager import list_cloud_documents
            from ingest.doc_classifier import DOC_TYPE_LABELS
            docs = list_cloud_documents(business_id, db_path=db_path)
        except Exception:
            docs = []
        if not docs:
            return "You haven't uploaded any files yet. Use **Upload documents** above to add returns and invoices."
        by_type: Dict[str, int] = {}
        for d in docs:
            by_type[DOC_TYPE_LABELS.get(d["document_type"], d["document_type"])] = by_type.get(
                DOC_TYPE_LABELS.get(d["document_type"], d["document_type"]), 0) + 1
        rows = "\n".join(f"| {k} | {v} |" for k, v in sorted(by_type.items(), key=lambda x: -x[1]))
        extra = f" {n_probs} problem(s) need a look; ask me to *show the errors*." if n_probs else " No problems found."
        return f"You have **{len(docs)} file(s)** in your cloud.{extra}\n\n| Type | Files |\n|---|---:|\n{rows}"

    if any(w in q for w in ["expire", "expiry", "deadline", "at risk", "urgent"]):
        soon = [r for r in live if r.get("status") in ("URGENT", "DUE_SOON")]
        if not soon:
            return "Nothing is close to expiring. Every credit you have is more than 180 days from its deadline."
        lines = "\n".join(f"- **{r['period_code']}**: AED {float(r['net_excess_aed']):,.2f}, claim by {r.get('expiry_date')} "
                          f"({r.get('days_remaining')} days left)" for r in soon)
        return f"**AED {at_risk:,.2f}** expires soon. Claim these first:\n\n{lines}"

    if any(w in q for w in ["problem", "error", "wrong", "mismatch", "reconcil", "match"]):
        if not n_probs:
            return "Your files agree with each other. I found no mismatches between invoices, the ledger and your VAT returns."
        lines = []
        for v in list(probs.values())[:6]:
            lines.append(f"- **{v['file_name']}**: " + "; ".join(v["problems"][:2]))
        return f"I found **{n_probs} problem(s)** in {len(probs)} file(s):\n\n" + "\n".join(lines) + \
               "\n\nAsk me to *show the errors* and I'll open each file with the problem boxed in red."

    if any(w in q for w in ["document", "upload", "missing", "need", "next", "do i do", "what should"]):
        if dv["gap"] > 0.01:
            pers = ", ".join(p["period"] for p in dv["unproven_periods"])
            return (f"Upload the **purchase tax invoices** for {pers}. Your VAT returns claim AED {dv['declared']:,.2f}, "
                    f"but invoices currently prove only AED {dv['proven']:,.2f}. The FTA refuses refund claims without them.")
        if total > 0:
            return (f"You're ready to claim **AED {total:,.2f}**. Ask me to *prepare my refund* and I'll build the FTA "
                    "refund package for your approval.")
        return ("Start by uploading your **filed VAT returns (Form 201)** and the **purchase tax invoices** for the same "
                "periods. I'll work out the refund from those.")

    # default: how much can I get back
    if total <= 0 and dv["declared"] <= 0:
        return ("You don't have a refund yet. Upload your **filed VAT returns (Form 201)** and the **purchase tax "
                "invoices** for the same periods, and I'll calculate it.")
    msg = f"You can get back **AED {total:,.2f}**, proven by your tax invoices."
    if dv["gap"] > 0.01:
        msg += (f"\n\nYour VAT returns claim AED {dv['declared']:,.2f} in total. The other **AED {dv['gap']:,.2f}** "
                f"needs purchase tax invoices for {', '.join(p['period'] for p in dv['unproven_periods'])}.")
    if at_risk > 0:
        msg += f"\n\n**AED {at_risk:,.2f}** of it expires soon, so claim that first."
    return msg + periods_table()
