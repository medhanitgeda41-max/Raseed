"""Pydantic v2 domain schemas for Raseed VAT recovery agent."""
from datetime import date, datetime
from typing import Literal, Optional, List
import re
from pydantic import BaseModel, Field, field_validator, model_validator


def validate_trn_string(v: Optional[str]) -> Optional[str]:
    """Validate that a TRN is exactly 15 digits starting with 100."""
    if v is None:
        return None
    v_clean = str(v).strip()
    if not re.match(r"^100\d{12}$", v_clean):
        raise ValueError(f"TRN must be exactly 15 digits and start with '100'. Received: {v_clean}")
    return v_clean


class Business(BaseModel):
    """Business entity / tenant."""
    id: Optional[int] = None
    name: str = Field(..., min_length=1)
    trn: str = Field(..., min_length=15, max_length=15)
    created_at: Optional[datetime] = None

    @field_validator("trn")
    @classmethod
    def check_trn(cls, v: str) -> str:
        res = validate_trn_string(v)
        if res is None:
            raise ValueError("TRN is required")
        return res


class User(BaseModel):
    """User account belonging to a specific business with strict role permissions."""
    id: Optional[int] = None
    business_id: int
    username: str = Field(..., min_length=2)
    role: Literal["OWNER", "ACCOUNTANT", "APPROVER"] = "ACCOUNTANT"
    created_at: Optional[datetime] = None


class BusinessProfile(BaseModel):
    """Statutory business profile of the taxable person."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    legal_name: str = Field(..., min_length=2, description="Registered legal name of the entity")
    trn: str = Field(..., description="15-digit Tax Registration Number")
    vat_registration_date: date = Field(..., description="Effective date of VAT registration")
    tax_period_type: Literal["monthly", "quarterly"] = Field("quarterly", description="Filing frequency")
    first_tax_period_start: date = Field(..., description="Start date of the first tax period")
    business_activity: str = Field(..., min_length=2, description="Primary commercial activity")
    emirate: Literal[
        "Abu Dhabi", "Dubai", "Sharjah", "Ajman", "Umm Al Quwain", "Ras Al Khaimah", "Fujairah"
    ] = Field(..., description="Emirate of registration")
    makes_exempt_supplies: bool = Field(False, description="Whether the business makes exempt supplies (apportionment)")
    apportionment_percentage: float = Field(100.0, ge=0.0, le=100.0, description="Recoverable input tax apportionment %")
    signatory_name: str = Field(..., min_length=2, description="Name of the authorized signatory")
    signatory_email: str = Field(..., description="Email of the authorized signatory")
    masked_iban: str = Field(..., description="Refund bank IBAN (masked for privacy)")

    @field_validator("trn")
    @classmethod
    def check_trn(cls, v: str) -> str:
        res = validate_trn_string(v)
        if res is None:
            raise ValueError("TRN is required")
        return res

    @field_validator("vat_registration_date", "first_tax_period_start")
    @classmethod
    def check_dates_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError(f"Date {v} cannot be in the future")
        return v

    @field_validator("signatory_email")
    @classmethod
    def check_email(cls, v: str) -> str:
        pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
        if not re.match(pattern, v):
            raise ValueError(f"Invalid email address: {v}")
        return v

    @field_validator("masked_iban")
    @classmethod
    def check_iban(cls, v: str) -> str:
        v_clean = v.strip().replace(" ", "")
        if len(v_clean) < 10:
            raise ValueError("IBAN must be at least 10 characters")
        return v_clean


class UploadedDocument(BaseModel):
    """Metadata for uploaded files stored locally."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    file_hash: str = Field(..., min_length=64, max_length=64, description="SHA-256 hash of file content")
    original_filename: str = Field(..., min_length=1)
    storage_path: str = Field(..., min_length=1)
    document_type: Literal["invoice", "register", "vat_return", "ledger", "fta_guide", "other"]
    file_size_bytes: int = Field(..., ge=0)
    mime_type: str
    uploaded_at: datetime = Field(default_factory=datetime.now)


class Invoice(BaseModel):
    """Full or simplified tax invoice."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    document_id: Optional[int] = None
    direction: Literal["purchase", "sales"] = Field("purchase", description="Invoice direction")
    supplier_name: str = Field(..., min_length=1)
    supplier_trn: Optional[str] = None
    customer_name: Optional[str] = None
    customer_trn: Optional[str] = None
    invoice_number: str = Field(..., min_length=1)
    date: date
    taxable_amount: float = Field(..., ge=0.0, description="Taxable supply amount in AED")
    vat_amount: float = Field(..., ge=0.0, description="VAT amount in AED")
    total: float = Field(..., ge=0.0, description="Total invoice amount in AED")
    currency: str = Field("AED", min_length=3, max_length=3)
    description_lines: List[str] = Field(default_factory=list)
    source_document: Optional[str] = None
    extraction_confidence: float = Field(1.0, ge=0.0, le=1.0)
    eligibility_status: Literal["VALID", "INELIGIBLE", "NEEDS_REVIEW", "DUPLICATE"] = Field(
        "VALID", description="VAT claim eligibility status"
    )
    eligibility_reason: Optional[str] = None

    @field_validator("supplier_trn", "customer_trn")
    @classmethod
    def check_trns(cls, v: Optional[str]) -> Optional[str]:
        return validate_trn_string(v)

    @field_validator("date")
    @classmethod
    def check_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError(f"Invoice date {v} cannot be in the future")
        return v

    @model_validator(mode="after")
    def verify_invoice_math(self) -> "Invoice":
        expected_total = round(self.taxable_amount + self.vat_amount, 2)
        diff = abs(round(self.total, 2) - expected_total)
        if diff > 0.05:
            raise ValueError(
                f"Total ({self.total}) does not match Taxable ({self.taxable_amount}) + VAT ({self.vat_amount}) "
                f"within tolerance (diff: {diff:.2f})"
            )
        return self


class Transaction(BaseModel):
    """Classified financial transaction with recovery apportionment."""
    id: Optional[int] = None
    business_id: int
    invoice_id: Optional[int] = None
    transaction_type: Literal[
        "standard_rated", "zero_rated", "exempt", "import",
        "reverse_charge", "credit_note_purchase", "credit_note_sales",
        "out_of_scope", "special_review"
    ] = "standard_rated"
    tax_period: str
    transaction_date: date
    taxable_amount: float
    vat_rate: float = 0.05
    vat_amount: float
    is_recoverable: bool = True
    recovery_percentage: float = Field(100.0, ge=0.0, le=100.0)
    recoverable_vat_amount: float
    notes: Optional[str] = None


class CreditLedgerEntry(BaseModel):
    """Transaction-level VAT credit record tracking life-cycle allocations."""
    id: Optional[int] = None
    business_id: int
    original_tax_period: str
    source_transaction_id: Optional[int] = None
    source_invoice_id: Optional[int] = None
    original_input_vat: float = Field(..., ge=0.0)
    eligible_recoverable_vat: float = Field(..., ge=0.0)
    adjustments: float = 0.0
    credit_used: float = Field(0.0, ge=0.0)
    refunds_requested: float = Field(0.0, ge=0.0)
    refunds_received: float = Field(0.0, ge=0.0)
    remaining_balance: float = Field(..., ge=0.0)
    expiry_date: date
    status: Literal["ACTIVE", "USED", "REFUND_REQUESTED", "REFUNDED", "EXPIRED"] = "ACTIVE"


class CreditAllocation(BaseModel):
    """Single-allocation entry preventing double-counting of credits."""
    id: Optional[int] = None
    business_id: int
    credit_ledger_entry_id: int
    allocation_type: Literal["CARRY_FORWARD_USE", "REFUND_CLAIM", "REFUND_SETTLEMENT", "EXPIRY_WRITE_OFF"]
    allocated_amount: float = Field(..., gt=0.0)
    target_period_code: Optional[str] = None
    refund_record_id: Optional[int] = None
    allocation_ref: str
    allocated_at: Optional[datetime] = None


class LedgerEntry(BaseModel):
    """Individual general ledger entry for VAT accounts."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    document_id: Optional[int] = None
    entry_date: date
    account_code: str = Field(..., min_length=1)
    account_name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    debit_aed: float = Field(0.0, ge=0.0)
    credit_aed: float = Field(0.0, ge=0.0)
    vat_code: Optional[str] = None
    reconciled_invoice_id: Optional[int] = None


class VatReturn(BaseModel):
    """Filed Form 201 VAT Return declaration."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    document_id: Optional[int] = None
    period: str = Field(..., description="Tax period code, e.g. 2024-Q1 or 2024-01")
    period_start: date
    period_end: date
    output_tax: float = Field(..., ge=0.0, description="Total Output Tax declared in AED")
    input_tax: float = Field(..., ge=0.0, description="Total Recoverable Input Tax declared in AED")
    net_payable_refundable: float = Field(..., description="Net VAT (Output - Input), negative indicates refundable")
    filing_date: Optional[date] = None

    @model_validator(mode="after")
    def verify_period_and_math(self) -> "VatReturn":
        if self.period_end < self.period_start:
            raise ValueError(f"Period end date ({self.period_end}) cannot be before start date ({self.period_start})")
        expected_net = round(self.output_tax - self.input_tax, 2)
        diff = abs(round(self.net_payable_refundable, 2) - expected_net)
        if diff > 0.05:
            raise ValueError(
                f"Net payable/refundable ({self.net_payable_refundable}) does not match Output ({self.output_tax}) "
                f"- Input ({self.input_tax}) within tolerance (diff: {diff:.2f})"
            )
        return self


class RefundRecord(BaseModel):
    """Official VAT refund application record."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    period_codes: List[str] = Field(..., min_length=1)
    total_refund_claimed_aed: float = Field(..., ge=0.0)
    emartax_application_ref: Optional[str] = None
    submission_date: Optional[date] = None
    fta_status: Literal[
        "DRAFTED", "SUBMITTED", "IN_REVIEW", "ADDITIONAL_INFO_REQUESTED", "APPROVED", "REJECTED"
    ] = "DRAFTED"
    fta_decision_date: Optional[date] = None
    amount_approved_aed: Optional[float] = Field(None, ge=0.0)


class CreditLedgerRow(BaseModel):
    """Period-by-period recoverable input VAT credit position."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    period_code: str = Field(..., min_length=1)
    period_end_date: date
    output_tax_aed: float = Field(0.0, ge=0.0)
    verified_input_tax_aed: float = Field(0.0, ge=0.0)
    net_excess_aed: float = Field(..., ge=0.0, description="Excess recoverable input VAT (credits)")
    expiry_date: date
    transitional_deadline_applicable: bool = False
    days_remaining: Optional[int] = None
    status: Literal["OK", "DUE_SOON", "URGENT", "EXPIRED", "CLAIMED"] = "OK"
    claimed_refund_amount_aed: float = Field(0.0, ge=0.0)


class Deadline(BaseModel):
    """Statutory deadline evaluation for a specific credit period."""
    period_code: str
    period_end_date: date
    standard_expiry_date: date
    effective_expiry_date: date
    is_transitional: bool
    days_remaining: int
    status: Literal["OK", "DUE_SOON", "URGENT", "EXPIRED"]


class DocumentRequest(BaseModel):
    """Request generated by the agent for missing tax invoices or documents."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    period_code: str
    invoice_reference: Optional[str] = None
    supplier_name: Optional[str] = None
    amount_aed: Optional[float] = Field(None, ge=0.0)
    reason: str = Field(..., min_length=3)
    status: Literal["OPEN", "FULFILLED", "CANCELLED"] = "OPEN"
    requested_at: datetime = Field(default_factory=datetime.now)
    fulfilled_at: Optional[datetime] = None


class AgentTask(BaseModel):
    """Internal task tracking for the autonomous loop."""
    id: Optional[int] = None
    business_id: Optional[int] = None
    task_type: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    priority: Literal["LOW", "MEDIUM", "HIGH", "URGENT"] = "MEDIUM"
    status: Literal["PENDING", "IN_PROGRESS", "WAITING_USER", "WAITING_APPROVAL", "DONE", "CANCELLED"] = "PENDING"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
