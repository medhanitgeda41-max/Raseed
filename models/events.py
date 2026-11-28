"""Audit log, staged actions, and state events for Raseed."""
from datetime import datetime
from typing import Literal, Optional, Any, Dict
from pydantic import BaseModel, Field


class StagedAction(BaseModel):
    """An action proposed by the agent or initiated by the user awaiting approval or auto-execution."""
    id: Optional[int] = None
    business_id: Optional[int] = 1
    staged_by_user_id: Optional[int] = None
    acting_user_role: Optional[str] = "OWNER"
    action_type: Literal[
        "REQUEST_DOCUMENT_FROM_USER",
        "MARK_INVOICE_STATUS",
        "PREPARE_REFUND_PACKAGE",
        "SUBMIT_REFUND_APPLICATION",
        "SEND_EXTERNAL_EMAIL",
        "RECORD_FTA_REFERENCE",
    ] = Field(..., description="Type of action being staged")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Action-specific parameters")
    rationale: str = Field(..., min_length=2, description="Why this action is proposed")
    risk_level: Literal["LOW", "CONSEQUENTIAL"] = Field(
        "CONSEQUENTIAL", description="Low risk auto-executes; consequential halts for human review"
    )
    status: Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "AUTO_EXECUTED"] = Field(
        "PENDING_APPROVAL", description="Approval lifecycle state"
    )
    guard_results: Dict[str, Any] = Field(default_factory=dict, description="Validation outcomes from safety guards")
    approver_note: Optional[str] = None
    staged_at: datetime = Field(default_factory=datetime.now)
    decided_at: Optional[datetime] = None


class AuditEvent(BaseModel):
    """Immutable audit log entry recording agent decisions, tool invocations, and user actions."""
    id: Optional[int] = None
    business_id: Optional[int] = 1
    user_id: Optional[int] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    actor: Literal["AGENT", "USER", "SYSTEM"] = Field("AGENT")
    event_type: str = Field(..., min_length=2, description="Identifier of the event")
    details: Dict[str, Any] = Field(default_factory=dict, description="Contextual parameters and metadata")
    llm_provider: Optional[str] = None
    execution_time_ms: Optional[float] = None
