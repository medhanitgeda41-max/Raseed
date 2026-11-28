"""State definition for Raseed LangGraph Autonomous VAT Recovery Agent."""
from typing import TypedDict, List, Dict, Any, Optional


class AgentState(TypedDict, total=False):
    """Complete persistent state of the LangGraph agent per business session."""

    # Tenant and Authentication
    business_id: int
    user_id: Optional[int]
    user_role: str  # 'OWNER', 'ACCOUNTANT', 'APPROVER'

    # Session and turn tracking
    turn_count: int
    step_count: int
    replan_count: int
    retry_count: int

    # Conversation history
    messages: List[Dict[str, Any]]

    # Onboarding status
    onboarding_complete: bool
    onboarding_missing_items: List[str]
    user_prompt_needed: Optional[str]

    # Recovery Plan & Transaction Tracking
    recovery_plan: List[Dict[str, Any]]
    current_step: Optional[Dict[str, Any]]

    # Action Staging and Governance
    proposed_action: Optional[Dict[str, Any]]
    guard_result: Optional[Dict[str, Any]]
    execution_result: Optional[Dict[str, Any]]

    # Evaluation and Escalation
    evaluation: Optional[Dict[str, Any]]
    escalated: bool
    escalation_reason: Optional[str]

    # Human-in-the-Loop Interruption
    interrupted_for_approval: bool
    pending_action_id: Optional[int]
    approval_decision: Optional[Dict[str, Any]]  # {"approved": bool, "user_role": str, "notes": str}
