"""Interactive terminal demo for Raseed — Autonomous VAT Recovery Agent.

Operates on the live local database with NO fake data.
If the workspace is empty, guides the user through onboarding.
Displays the transaction-level recovery plan, pending approvals, and ledger status.
"""
import sys
import argparse
from pathlib import Path
from typing import Optional
from langgraph.types import Command
from langgraph.checkpoint.memory import MemorySaver

from database.connection import init_db, get_or_create_default_business, get_db_connection
from onboarding.requirements import onboarding_status
from agent.graph import create_agent_graph
from tools.read_tools import get_credit_ledger, get_staged_actions


def print_banner():
    print("=" * 72)
    print("  RASEED (رصيد) — Autonomous VAT Recovery Agent for UAE Businesses")
    print("  Official Statutory Rules: Federal Decree-Law No. 8 of 2017 & DL 16-17/2025")
    print("=" * 72)


def run_cli(business_id: Optional[int] = None, role: str = "APPROVER", db_path: Optional[Path] = None):
    print_banner()

    # Ensure DB schema is ready
    init_db(db_path=db_path)
    biz_id = business_id or get_or_create_default_business(db_path=db_path)

    print(f"\n[Session Context] Business ID: #{biz_id} | Active Role: {role}")

    # Check onboarding readiness
    status = onboarding_status(business_id=biz_id, db_path=db_path)
    can_start = status.get("can_start", False)
    missing_items = status.get("missing_required", [])
    print(f"[Onboarding Status] Minimum Met: {'YES' if can_start else 'NO'}")

    if not can_start:
        print("\n" + "-" * 72)
        print("ACTION REQUIRED: Zero-Seed Workspace Detected")
        print("-" * 72)
        print("Raseed operates strictly on verified business records. Before starting recovery:")
        for missing in missing_items:
            if missing == "business_profile":
                print("  • Missing: Business Profile (TRN, VAT registration date, tax period type)")
            elif missing == "purchase_invoices":
                print("  • Missing: Purchase Invoices / Purchase Register (CSV, XLSX, or PDF)")
            elif missing == "vat_returns":
                print("  • Missing: Filed Form 201 VAT Returns for review periods")
        print("\nPlease upload documents or configure your business profile to begin.")
        print("-" * 72 + "\n")
        return

    # Compile agent graph
    checkpointer = MemorySaver()
    graph = create_agent_graph(db_path=db_path, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": f"cli_biz_{biz_id}"}}

    initial_state = {
        "business_id": biz_id,
        "user_role": role,
        "step_count": 0,
        "turn_count": 0,
    }

    print("\n[Agent] Initiating perception and recovery planning...")
    result = graph.invoke(initial_state, config=config)

    # Check for human-in-the-loop interrupt
    state_snap = graph.get_state(config)
    if state_snap.next:
        # Interrupted at a human approval checkpoint
        tasks = state_snap.tasks
        interrupt_val = None
        for t in tasks:
            if t.interrupts:
                interrupt_val = t.interrupts[0].value
                break

        print("\n" + "!" * 72)
        print("  HUMAN APPROVAL CHECKPOINT (CONSEQUENTIAL ACTION)")
        print("!" * 72)
        if interrupt_val:
            print(f"Action Type:  {interrupt_val.get('action_type')}")
            print(f"Action ID:    #{interrupt_val.get('action_id')}")
            print(f"Claim Amount: {interrupt_val.get('payload', {}).get('claimed_amount', 0.0)} AED")
            print(f"Details:      {interrupt_val.get('message')}")
        print("!" * 72)

        ans = input("\nDo you approve submitting this refund application to FTA? (yes/no): ").strip().lower()
        is_approved = ans in ("yes", "y")
        approver_note = input("Approval comment / reference note: ").strip() or "CLI review decision"

        resume_payload = {
            "approved": is_approved,
            "user_role": role,
            "notes": approver_note,
        }

        print(f"\n[Agent] Resuming execution with decision: {'APPROVED' if is_approved else 'REJECTED'}...")
        result = graph.invoke(Command(resume=resume_payload), config=config)

    # Display Recovery Plan
    plan = result.get("recovery_plan", [])
    print("\n" + "=" * 72)
    print("  CURRENT RECOVERY PLAN (Prioritized by Statutory Expiry)")
    print("=" * 72)
    if not plan:
        print("  No pending recovery tasks. All eligible credits verified and accounted for.")
    else:
        for idx, step in enumerate(plan, 1):
            prio = step.get("priority", "MEDIUM")
            st = step.get("status", "PENDING")
            amt = step.get("amount_aed", 0.0)
            dl = step.get("deadline") or "N/A"
            print(f"  {idx}. [{prio}] {step.get('description')}")
            print(f"     Amount: {amt} AED | Deadline: {dl} | Status: {st}")
    print("=" * 72)

    # Display Ledger Summary
    ledger = get_credit_ledger(business_id=biz_id, db_path=db_path)
    print("\n" + "=" * 72)
    print("  STATUTORY CREDIT LEDGER SUMMARY")
    print("=" * 72)
    if not ledger:
        print("  No credit ledger entries found.")
    else:
        for row in ledger:
            print(f"  Period: {row['period_code']} | Output: {row['output_tax_aed']} AED | "
                  f"Verified Input: {row['verified_input_tax_aed']} AED | "
                  f"Net Excess: {row['net_excess_aed']} AED | Expiry: {row['expiry_date']} ({row['status']})")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Raseed Autonomous VAT Recovery Agent CLI")
    parser.add_argument("--business-id", type=int, default=1, help="Business ID to operate on")
    parser.add_argument("--role", type=str, default="APPROVER", choices=["OWNER", "ACCOUNTANT", "APPROVER"], help="User role")
    parser.add_argument("--db-path", type=str, default=None, help="Custom SQLite DB path")
    args = parser.parse_args()

    custom_db = Path(args.db_path) if args.db_path else None
    run_cli(business_id=args.business_id, role=args.role, db_path=custom_db)
