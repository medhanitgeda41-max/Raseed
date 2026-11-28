"""LangGraph stateful workflow for Raseed Autonomous VAT Recovery Agent.

Goal: "Recover all eligible VAT credits before they expire"

Architecture:
- ONBOARD: checks onboarding readiness; if missing, asks user in plain language for 1-2 items and waits.
- PERCEIVE: ingests new uploads, validates, extracts, rebuilds ledger.
- PLAN: transaction-level reasoning; builds and updates ordered RECOVERY PLAN.
- GUARD: runs deterministic guards on proposed actions (injection, validator, refund guard).
- ROUTE: low-risk -> auto-executes; consequential -> LangGraph interrupt() for human approval.
- EVALUATE: deterministic code check: did the financial outcome change as expected? Retry, ask, escalate.
- REMEMBER: state, tasks, document requests and decisions stored in SQLite with LangGraph checkpointer.
"""
from pathlib import Path
from typing import Dict, Any, Optional, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from config.settings import settings
from database.connection import get_db_connection, get_or_create_default_business
from onboarding.requirements import onboarding_status
from guards.injection_filter import contains_injection, scan_and_quarantine
from guards.refund_guard import verify_refund_claim
from tools.compute_tools import rebuild_credit_ledger
from tools.write_gateway import stage_action, execute_approved_action, reject_staged_action
from agent.state import AgentState
from agent.recovery_planner import build_or_update_recovery_plan
from agent.evaluator import evaluate_action_result


def create_agent_graph(
    db_path: Optional[Path] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
) -> Any:
    """Factory creating the LangGraph compiled state graph for Raseed."""

    # --------------------------------------------------------------------------
    # NODE 1: ONBOARD
    # --------------------------------------------------------------------------
    def onboard_node(state: AgentState) -> Dict[str, Any]:
        if state.get("onboarding_complete"):
            return {
                "onboarding_complete": True,
                "onboarding_missing_items": [],
                "user_prompt_needed": None,
            }

        biz_id = state.get("business_id") or 1
        status = onboarding_status(business_id=biz_id, db_path=db_path)
        can_start = status.get("can_start", False)
        missing_items = status.get("missing_required", [])

        if not can_start:
            prompt = ""
            if "business_profile" in missing_items:
                prompt = (
                    "Welcome to Raseed. To begin recovering your UAE VAT credits, "
                    "please provide your business profile (legal name, 15-digit TRN starting with 100, "
                    "VAT registration date, and tax period frequency)."
                )
            elif "purchase_invoices" in missing_items and "vat_returns" in missing_items:
                prompt = (
                    "To review your VAT position, please upload your purchase invoices or purchase register "
                    "(CSV/XLSX or PDF), along with your filed Form 201 VAT returns."
                )
            elif "purchase_invoices" in missing_items:
                prompt = (
                    "Please upload your purchase invoices or purchase register (CSV/XLSX) "
                    "for the tax periods under review."
                )
            elif "vat_returns" in missing_items:
                prompt = (
                    "Please upload your filed Form 201 VAT returns so we can verify output tax "
                    "and calculate excess recoverable credits."
                )

            return {
                "onboarding_complete": False,
                "onboarding_missing_items": missing_items,
                "user_prompt_needed": prompt,
            }

        return {
            "onboarding_complete": True,
            "onboarding_missing_items": [],
            "user_prompt_needed": None,
        }

    # --------------------------------------------------------------------------
    # NODE 2: PERCEIVE
    # --------------------------------------------------------------------------
    def perceive_node(state: AgentState) -> Dict[str, Any]:
        biz_id = state.get("business_id") or 1
        # Synchronize credit ledger from verified invoices and filed returns
        rebuild_credit_ledger(business_id=biz_id, db_path=db_path)
        return {
            "step_count": state.get("step_count", 0),
        }

    # --------------------------------------------------------------------------
    # NODE 3: PLAN
    # --------------------------------------------------------------------------
    def plan_node(state: AgentState) -> Dict[str, Any]:
        biz_id = state.get("business_id") or 1
        plan = build_or_update_recovery_plan(business_id=biz_id, db_path=db_path)

        # Check turn safety step cap
        if state.get("step_count", 0) >= settings.max_steps_per_turn:
            return {
                "recovery_plan": plan,
                "proposed_action": None,
            }

        # If an action was explicitly proposed (e.g. from test or planner override), preserve it
        if state.get("proposed_action") and not state.get("guard_result"):
            return {
                "recovery_plan": plan,
                "proposed_action": state.get("proposed_action"),
            }

        # Find the next actionable step
        proposed = None
        for step in plan:
            st = step.get("status")
            t_type = step.get("task_type")

            if t_type == "REQUEST_MISSING_DOCUMENTS" and st == "PENDING":
                proposed = {
                    "action_type": "REQUEST_DOCUMENT_FROM_USER",
                    "payload": {
                        "period_code": step["period_code"],
                        "invoice_reference": f"INV-MISSING-{step['period_code']}",
                        "supplier_name": "Multiple Unverified Suppliers",
                        "amount_aed": step["amount_aed"],
                        "reason": step["description"],
                    },
                    "source": "AI proposal with verified tool data",
                    "step_ref": step,
                }
                break

            elif t_type == "RESOLVE_NEEDS_REVIEW" and st == "WAITING_APPROVAL":
                proposed = {
                    "action_type": "RESOLVE_NEEDS_REVIEW",
                    "payload": {
                        "invoice_id": step.get("invoice_id"),
                        "reason": step["description"],
                    },
                    "source": "AI proposal with verified tool data",
                    "step_ref": step,
                }
                break

            elif t_type == "DRAFT_REFUND_PACKAGE" and st == "PENDING":
                proposed = {
                    "action_type": "SUBMIT_REFUND_APPLICATION",
                    "payload": {
                        "claimed_amount": step["amount_aed"],
                        "period_codes": [step["period_code"]],
                    },
                    "source": "AI proposal with verified tool data",
                    "step_ref": step,
                }
                break

        return {
            "recovery_plan": plan,
            "proposed_action": proposed,
            "current_step": proposed.get("step_ref") if proposed else None,
        }

    # --------------------------------------------------------------------------
    # NODE 4: GUARD
    # --------------------------------------------------------------------------
    def guard_node(state: AgentState) -> Dict[str, Any]:
        proposed = state.get("proposed_action")
        if not proposed:
            return {"guard_result": {"passed": True}}

        action_type = proposed.get("action_type")
        payload = proposed.get("payload", {})
        biz_id = state.get("business_id") or 1

        # 1. Prompt Injection Filter on payload fields and reason
        payload_text = str(payload) + " " + str(proposed.get("source", ""))
        if contains_injection(payload_text):
            sanitized, is_quarantined, q_reason = scan_and_quarantine(payload_text)
            return {
                "guard_result": {"passed": False, "reason": f"Security Alert: {q_reason}."},
                "escalated": True,
                "escalation_reason": "Quarantined adversarial instructions detected in proposed action payload.",
            }

        # 2. Refund Guard on refund applications
        if action_type == "SUBMIT_REFUND_APPLICATION":
            claimed_amt = float(payload.get("claimed_amount", 0.0))
            periods = payload.get("period_codes", [])

            is_valid, reason = verify_refund_claim(
                claimed_amount=claimed_amt,
                period_codes=periods,
                business_id=biz_id,
                db_path=db_path,
            )

            if not is_valid:
                new_replan = state.get("replan_count", 0) + 1
                return {
                    "guard_result": {"passed": False, "reason": reason},
                    "replan_count": new_replan,
                    "escalated": new_replan > settings.max_replans,
                    "escalation_reason": f"Refund guard repeatedly failed: {reason}" if new_replan > settings.max_replans else None,
                }

        return {
            "guard_result": {"passed": True, "reason": "All deterministic guards passed."},
        }

    # --------------------------------------------------------------------------
    # NODE 5: ROUTE & ACT
    # --------------------------------------------------------------------------
    def route_node(state: AgentState) -> Dict[str, Any]:
        proposed = state.get("proposed_action")
        if not proposed:
            return {}

        guard_res = state.get("guard_result", {})
        if not guard_res.get("passed"):
            return {}

        action_type = proposed.get("action_type")
        payload = proposed.get("payload", {})
        biz_id = state.get("business_id") or 1
        user_role = state.get("user_role") or "OWNER"

        # Consequential Actions -> Human Approval via interrupt()
        consequential_types = {"SUBMIT_REFUND_APPLICATION", "RESOLVE_NEEDS_REVIEW"}
        is_consequential = (
            action_type in consequential_types
            or float(payload.get("claimed_amount", 0.0)) > settings.approval_threshold_aed
        )

        if not is_consequential:
            # Low-risk action: auto-execute through gateway
            action_id, status = stage_action(
                action_type=action_type,
                payload=payload,
                reason=proposed.get("source", "Automated routine task"),
                business_id=biz_id,
                source="AGENT",
                db_path=db_path,
            )
            return {
                "execution_result": {"success": True, "status": status, "action_id": action_id},
                "step_count": state.get("step_count", 0) + 1,
            }

        # Consequential Action: Stage in database as PENDING_APPROVAL
        action_id, status = stage_action(
            action_type=action_type,
            payload=payload,
            reason=proposed.get("source", "Consequential VAT recovery proposal"),
            business_id=biz_id,
            source="AGENT",
            db_path=db_path,
        )

        # Halt and wait for human decision
        interrupt_payload = {
            "action_id": action_id,
            "action_type": action_type,
            "payload": payload,
            "business_id": biz_id,
            "message": f"Consequential approval required for {action_type} (Amount: {payload.get('claimed_amount', 0.0)} AED).",
        }

        # LangGraph interrupt primitive
        decision = interrupt(interrupt_payload)

        # Resumed from human decision
        decision_dict = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = decision_dict.get("approved", False)
        approver_role = decision_dict.get("user_role") or user_role

        # Enforce RBAC: ACCOUNTANT cannot approve refund submission
        if approver_role == "ACCOUNTANT" and approved:
            return {
                "execution_result": {
                    "success": False,
                    "status": "BLOCKED_PERMISSION",
                    "message": "Role permission violation: ACCOUNTANT cannot approve refund application. APPROVER role required.",
                },
                "escalated": True,
                "escalation_reason": "Role permission violation: ACCOUNTANT attempted to approve consequential action.",
            }

        if approved:
            # Execute approved action with re-verification of guards
            success, msg = execute_approved_action(
                action_id=action_id,
                approver_note=decision_dict.get("notes", "Approved by human reviewer"),
                approver_role=approver_role,
                db_path=db_path,
            )
            return {
                "execution_result": {"success": success, "status": "EXECUTED" if success else "FAILED", "message": msg, "action_id": action_id},
                "step_count": state.get("step_count", 0) + 1,
            }
        else:
            success, msg = reject_staged_action(
                action_id=action_id,
                rejection_note=decision_dict.get("notes", "Rejected by user"),
                db_path=db_path,
            )
            return {
                "execution_result": {"success": True, "status": "REJECTED", "message": msg, "action_id": action_id},
            }

    # --------------------------------------------------------------------------
    # NODE 6: EVALUATE
    # --------------------------------------------------------------------------
    def evaluate_node(state: AgentState) -> Dict[str, Any]:
        proposed = state.get("proposed_action")
        exec_res = state.get("execution_result")
        biz_id = state.get("business_id") or 1

        if not proposed or not exec_res:
            return {}

        action_type = proposed.get("action_type")
        payload = proposed.get("payload", {})
        retry_count = state.get("retry_count", 0)

        eval_res = evaluate_action_result(
            action_type=action_type,
            payload=payload,
            execution_result=exec_res,
            business_id=biz_id,
            retry_count=retry_count,
            db_path=db_path,
        )

        new_plan = build_or_update_recovery_plan(business_id=biz_id, db_path=db_path)

        updates: Dict[str, Any] = {
            "evaluation": eval_res.to_dict(),
            "recovery_plan": new_plan,
        }

        if eval_res.requires_escalation:
            updates["escalated"] = True
            updates["escalation_reason"] = eval_res.reason

        if eval_res.retry_suggested:
            updates["retry_count"] = retry_count + 1

        return updates

    # --------------------------------------------------------------------------
    # NODE 7: REMEMBER
    # --------------------------------------------------------------------------
    def remember_node(state: AgentState) -> Dict[str, Any]:
        return {
            "turn_count": state.get("turn_count", 0) + 1,
        }

    # --------------------------------------------------------------------------
    # CONDITIONAL EDGES
    # --------------------------------------------------------------------------
    def after_onboard_edge(state: AgentState) -> Literal["perceive", "remember"]:
        if state.get("onboarding_complete"):
            return "perceive"
        return "remember"

    def after_plan_edge(state: AgentState) -> Literal["guard", "remember"]:
        if state.get("escalated"):
            return "remember"
        if state.get("proposed_action"):
            return "guard"
        return "remember"

    def after_guard_edge(state: AgentState) -> Literal["route", "plan", "remember"]:
        if state.get("escalated"):
            return "remember"
        guard_res = state.get("guard_result", {})
        if guard_res.get("passed"):
            return "route"
        if state.get("replan_count", 0) <= settings.max_replans:
            return "plan"
        return "remember"

    def after_evaluate_edge(state: AgentState) -> Literal["plan", "remember"]:
        if state.get("escalated"):
            return "remember"
        if state.get("step_count", 0) >= settings.max_steps_per_turn:
            return "remember"
        # Check if plan still has actionable pending items
        plan = state.get("recovery_plan", [])
        has_pending = any(s.get("status") in ("PENDING", "WAITING_APPROVAL") for s in plan)
        if has_pending:
            return "plan"
        return "remember"

    # --------------------------------------------------------------------------
    # ASSEMBLE GRAPH
    # --------------------------------------------------------------------------
    builder = StateGraph(AgentState)

    builder.add_node("onboard", onboard_node)
    builder.add_node("perceive", perceive_node)
    builder.add_node("plan", plan_node)
    builder.add_node("guard", guard_node)
    builder.add_node("route", route_node)
    builder.add_node("evaluate", evaluate_node)
    builder.add_node("remember", remember_node)

    builder.add_edge(START, "onboard")
    builder.add_conditional_edges("onboard", after_onboard_edge, {"perceive": "perceive", "remember": "remember"})
    builder.add_edge("perceive", "plan")
    builder.add_conditional_edges("plan", after_plan_edge, {"guard": "guard", "remember": "remember"})
    builder.add_conditional_edges("guard", after_guard_edge, {"route": "route", "plan": "plan", "remember": "remember"})
    builder.add_edge("route", "evaluate")
    builder.add_conditional_edges("evaluate", after_evaluate_edge, {"plan": "plan", "remember": "remember"})
    builder.add_edge("remember", END)

    saver = checkpointer or MemorySaver()
    return builder.compile(checkpointer=saver)
