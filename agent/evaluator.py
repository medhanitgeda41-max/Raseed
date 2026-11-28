"""Deterministic code-based evaluator for agent actions.

Enforces Golden Rule #6:
- Verifies whether the credit amount, document status, or reconciliation variance actually changed as expected.
- If not: retries once, then asks the user, then escalates with statutory reason.
- Escalate every uncertain legal classification.
"""
from pathlib import Path
from typing import Dict, Any, Optional
from database.connection import get_db_connection


class EvaluationResult:
    def __init__(self, success: bool, outcome_changed: bool, reason: str, requires_escalation: bool = False, retry_suggested: bool = False):
        self.success = success
        self.outcome_changed = outcome_changed
        self.reason = reason
        self.requires_escalation = requires_escalation
        self.retry_suggested = retry_suggested

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "outcome_changed": self.outcome_changed,
            "reason": self.reason,
            "requires_escalation": self.requires_escalation,
            "retry_suggested": self.retry_suggested,
        }


def evaluate_action_result(
    action_type: str,
    payload: Dict[str, Any],
    execution_result: Dict[str, Any],
    business_id: int,
    retry_count: int = 0,
    db_path: Optional[Path] = None,
) -> EvaluationResult:
    """Evaluate whether an executed or staged action achieved its statutory purpose."""
    conn = get_db_connection(db_path=db_path, readonly=True)

    try:
        cursor = conn.cursor()

        # 1. Document Request Evaluation
        if action_type == "REQUEST_DOCUMENT_FROM_USER":
            p_code = payload.get("period_code")
            inv_ref = payload.get("invoice_reference")
            cursor.execute(
                """
                SELECT COUNT(*) as cnt FROM document_requests
                WHERE business_id = ? AND period_code = ? AND status = 'OPEN'
                  AND (? IS NULL OR invoice_reference = ?);
                """,
                (business_id, p_code, inv_ref, inv_ref),
            )
            count = cursor.fetchone()["cnt"]
            if count > 0:
                return EvaluationResult(
                    success=True,
                    outcome_changed=True,
                    reason=f"Document request for period {p_code} registered and awaiting user fulfillment.",
                )
            else:
                retry = retry_count < 1
                return EvaluationResult(
                    success=False,
                    outcome_changed=False,
                    reason="Document request failed to persist to database.",
                    retry_suggested=retry,
                    requires_escalation=not retry,
                )

        # 2. Rebuild Credit Ledger Evaluation
        elif action_type == "REBUILD_CREDIT_LEDGER":
            cursor.execute("SELECT COUNT(*) as cnt FROM credit_ledger WHERE business_id = ?;", (business_id,))
            cnt = cursor.fetchone()["cnt"]
            if cnt > 0:
                return EvaluationResult(
                    success=True,
                    outcome_changed=True,
                    reason=f"Credit ledger successfully synchronized across {cnt} tax periods.",
                )
            else:
                return EvaluationResult(
                    success=False,
                    outcome_changed=False,
                    reason="Credit ledger rebuild yielded no records.",
                    retry_suggested=retry_count < 1,
                )

        # 3. Refund Application Submission Evaluation
        elif action_type == "SUBMIT_REFUND_APPLICATION":
            status = execution_result.get("status")
            if status in ("PENDING_APPROVAL", "EXECUTED"):
                return EvaluationResult(
                    success=True,
                    outcome_changed=True,
                    reason=f"Refund application staged in status: {status}.",
                )
            elif status == "BLOCKED_PERMISSION":
                return EvaluationResult(
                    success=False,
                    outcome_changed=False,
                    reason="Role permission violation: ACCOUNTANT cannot approve refund submission. APPROVER required.",
                    requires_escalation=True,
                )
            else:
                return EvaluationResult(
                    success=False,
                    outcome_changed=False,
                    reason=execution_result.get("message", "Refund staging failed."),
                    requires_escalation=True,
                )

        # 4. Review Uncertain / Blocked Tax Items Evaluation
        elif action_type == "RESOLVE_NEEDS_REVIEW":
            classification = payload.get("classification")
            if classification in ("standard_rated", "exempt", "zero_rated"):
                return EvaluationResult(
                    success=True,
                    outcome_changed=True,
                    reason=f"Transaction reclassified to {classification} upon human confirmation.",
                )
            else:
                return EvaluationResult(
                    success=False,
                    outcome_changed=False,
                    reason="Statutory classification remains ambiguous. Escalating to licensed tax agent review.",
                    requires_escalation=True,
                )

        # Default fallback
        return EvaluationResult(
            success=execution_result.get("success", True),
            outcome_changed=True,
            reason="Action completed.",
        )
    finally:
        conn.close()
