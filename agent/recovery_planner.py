"""Transaction-level reasoning and recovery plan builder.

Builds and persists an ordered RECOVERY PLAN:
1. Fix missing documents for credits expiring soonest first (URGENT < 60 days, DUE_SOON < 180 days).
2. Resolve NEEDS_REVIEW items (blocked categories or ambiguous classifications).
3. Reconcile ledger and filing differences.
4. Prepare refund packages per tax period (ordered by deadline ascending, then amount descending).

Ensures idempotency after app restarts: does not recreate or repeat finished steps.
"""
from datetime import date
from pathlib import Path
from typing import List, Dict, Any, Optional
from database.connection import get_db_connection
from tools.compute_tools import rebuild_credit_ledger, reconcile_4_way


def build_or_update_recovery_plan(
    business_id: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Build or synchronize the persistent recovery plan for a business."""
    conn = get_db_connection(db_path=db_path, readonly=False)

    try:
        cursor = conn.cursor()

        # 1. Fetch current credit ledger status (rebuild if needed)
        cursor.execute(
            """
            SELECT period_code, period_end_date, net_excess_aed, expiry_date, days_remaining, status
            FROM credit_ledger
            WHERE business_id = ?
            ORDER BY expiry_date ASC, net_excess_aed DESC;
            """,
            (business_id,),
        )
        ledger_rows = [dict(r) for r in cursor.fetchall()]

        # 2. Fetch existing persistent tasks from agent_tasks
        cursor.execute(
            """
            SELECT id, task_type, description, priority, status
            FROM agent_tasks
            WHERE business_id = ?;
            """,
            (business_id,),
        )
        existing_tasks = [dict(r) for r in cursor.fetchall()]
        task_map = {f"{t['task_type']}::{t['description']}": t for t in existing_tasks}

        # 3. Check for open document requests
        cursor.execute(
            """
            SELECT period_code, invoice_reference, amount_aed, reason, status
            FROM document_requests
            WHERE business_id = ?;
            """,
            (business_id,),
        )
        doc_requests = [dict(r) for r in cursor.fetchall()]
        open_doc_periods = {d["period_code"] for d in doc_requests if d["status"] == "OPEN"}

        # 4. Check unreviewed / blocked invoices
        cursor.execute(
            """
            SELECT id, invoice_number, date, vat_amount, eligibility_status, eligibility_reason
            FROM invoices
            WHERE business_id = ? AND (eligibility_status = 'NEEDS_REVIEW' OR (eligibility_status = 'DUPLICATE' AND COALESCE(eligibility_reason, '') NOT LIKE 'Also in %'));
            """,
            (business_id,),
        )
        unreviewed_invoices = [dict(r) for r in cursor.fetchall()]

        # 5. Check 4-way reconciliation discrepancies
        recon = reconcile_4_way(business_id=business_id, db_path=db_path)
        discrepancies = recon.get("discrepancies", [])

        plan_steps: List[Dict[str, Any]] = []

        # ======================================================================
        # Priority 1: Fix missing documents for credits expiring soonest
        # ======================================================================
        # Find periods where declared input tax exceeds verified invoices
        cursor.execute(
            """
            SELECT vr.period, vr.period_end, vr.input_tax,
                   COALESCE(SUM(inv.vat_amount), 0.0) as verified_vat
            FROM vat_returns vr
            LEFT JOIN invoices inv ON vr.business_id = inv.business_id
                 AND inv.direction = 'purchase'
                 AND inv.eligibility_status = 'VALID'
                 AND inv.date >= vr.period_start AND inv.date <= vr.period_end
            WHERE vr.business_id = ?
            GROUP BY vr.period, vr.period_end, vr.input_tax;
            """,
            (business_id,),
        )
        period_invoices = cursor.fetchall()
        for pi in period_invoices:
            p_code = pi["period"]
            input_tax = round(float(pi["input_tax"]), 2)
            verified_vat = round(float(pi["verified_vat"]), 2)

            if input_tax > verified_vat + 0.05:
                missing_amount = round(input_tax - verified_vat, 2)
                desc = f"Request missing tax invoices for period {p_code} (unverified input VAT: {missing_amount} AED)"
                key = f"REQUEST_MISSING_DOCUMENTS::{desc}"

                # Find corresponding ledger entry for expiry
                led = next((l for l in ledger_rows if l["period_code"] == p_code), None)
                prio = "URGENT" if led and led.get("status") == "URGENT" else "HIGH"
                deadline = led.get("expiry_date") if led else "2026-12-31"

                # Check if already fulfilled or exists
                status = "PENDING"
                if p_code in open_doc_periods:
                    status = "WAITING_USER"
                elif key in task_map:
                    status = task_map[key]["status"]

                step_dict = {
                    "task_type": "REQUEST_MISSING_DOCUMENTS",
                    "period_code": p_code,
                    "description": desc,
                    "amount_aed": missing_amount,
                    "deadline": deadline,
                    "priority": prio,
                    "status": status,
                }
                plan_steps.append(step_dict)

        # ======================================================================
        # Priority 2: Resolve NEEDS_REVIEW items
        # ======================================================================
        for unrec in unreviewed_invoices:
            inv_no = unrec["invoice_number"]
            amt = float(unrec["vat_amount"])
            desc = f"Human tax review for invoice {inv_no} ({unrec['eligibility_reason']})"
            key = f"RESOLVE_NEEDS_REVIEW::{desc}"

            status = "WAITING_APPROVAL"
            if key in task_map:
                status = task_map[key]["status"]

            plan_steps.append({
                "task_type": "RESOLVE_NEEDS_REVIEW",
                "period_code": None,
                "description": desc,
                "amount_aed": amt,
                "deadline": None,
                "priority": "HIGH",
                "status": status,
                "invoice_id": unrec["id"],
            })

        # ======================================================================
        # Priority 3: Reconcile differences
        # ======================================================================
        for disc in discrepancies:
            cause = disc["cause"]
            if cause not in ("duplicate", "timing_difference"):
                continue
            desc = f"Reconcile {cause}: {disc['description']}"
            key = f"RECONCILE_DIFFERENCES::{desc}"

            status = "PENDING"
            if key in task_map:
                status = task_map[key]["status"]

            plan_steps.append({
                "task_type": "RECONCILE_DIFFERENCES",
                "period_code": disc.get("period_code"),
                "description": desc,
                "amount_aed": disc.get("amount_aed", 0.0),
                "deadline": None,
                "priority": "MEDIUM",
                "status": status,
            })

        # ======================================================================
        # Priority 4: Prepare refund packages per tax period
        # ======================================================================
        for row in ledger_rows:
            p_code = row["period_code"]
            excess = float(row["net_excess_aed"])
            st = row["status"]

            # Only prepare refund if excess credit exists and period is not already claimed
            if excess > 0 and st != "CLAIMED":
                # Check if this period has blocking missing documents
                has_open_docs = any(s["period_code"] == p_code and s["status"] in ("PENDING", "WAITING_USER") for s in plan_steps)

                desc = f"Prepare EmaraTax VAT refund package for period {p_code} (Claimable: {excess} AED)"
                key = f"DRAFT_REFUND_PACKAGE::{desc}"

                task_status = "PENDING"
                if key in task_map:
                    task_status = task_map[key]["status"]
                elif has_open_docs:
                    task_status = "WAITING_USER"

                prio = "URGENT" if st == "URGENT" else ("HIGH" if st == "DUE_SOON" else "MEDIUM")

                plan_steps.append({
                    "task_type": "DRAFT_REFUND_PACKAGE",
                    "period_code": p_code,
                    "description": desc,
                    "amount_aed": excess,
                    "deadline": row["expiry_date"],
                    "priority": prio,
                    "status": task_status,
                })

        # ======================================================================
        # Sync to SQLite agent_tasks table (idempotent upsert)
        # ======================================================================
        with conn:
            for s in plan_steps:
                key = f"{s['task_type']}::{s['description']}"
                if key not in task_map:
                    cursor.execute(
                        """
                        INSERT INTO agent_tasks (business_id, task_type, description, priority, status)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (business_id, s["task_type"], s["description"], s["priority"], s["status"]),
                    )
                    s["id"] = cursor.lastrowid
                else:
                    s["id"] = task_map[key]["id"]
                    if task_map[key]["status"] != s["status"]:
                        cursor.execute(
                            "UPDATE agent_tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                            (s["status"], s["id"]),
                        )

        # Sort entire plan by priority and deadline
        priority_weights = {"URGENT": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
        plan_steps.sort(
            key=lambda x: (
                -priority_weights.get(x["priority"], 0),
                x["deadline"] or "9999-12-31",
                -x.get("amount_aed", 0.0),
            )
        )

        return plan_steps
    finally:
        conn.close()
