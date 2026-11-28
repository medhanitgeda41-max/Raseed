"""Deterministic transaction classification guard.

Enforces statutory VAT classifications under Federal Decree-Law No. 8 of 2017:
- standard_rated (5%)
- zero_rated (0%)
- exempt (0% - no input tax recovery)
- import (reciprocal output and input accounting)
- reverse_charge (Article 48)
- credit_note_purchase (reduces input tax)
- credit_note_sales (reduces output tax)
- out_of_scope
- special_review (uncertain items escalated to human review)

Golden Rule: The LLM may suggest a type; pure Python code applies deterministic rules and decides.
"""
import re
from typing import Tuple, Optional


ZERO_RATED_PATTERNS = [
    r"\bexport\b", r"\bzero[-_ ]?rated?\b", r"\bhealthcare\b", r"\bpharmaceutical\b",
    r"\bbasic education\b", r"\binternational transport\b", r"\bprecious metals?\b",
]

EXEMPT_PATTERNS = [
    r"\bexempt\b", r"\bfinancial services?\b", r"\blife insurance\b",
    r"\bresidential\b", r"\bresidential lease\b", r"\bresidential rent\b", r"\bbare land\b",
    r"\blocal passenger transport\b",
]

REVERSE_CHARGE_PATTERNS = [
    r"\breverse[-_ ]?charge\b", r"\brcm\b", r"\bimported services?\b",
    r"\bnon[-_ ]?resident supplier\b", r"\bforeign consulting\b",
]

IMPORT_PATTERNS = [
    r"\bcustoms\b", r"\bimport duty\b", r"\bbill of entry\b",
    r"\bdeclaration 701\b", r"\bport authority\b", r"\bimported goods\b",
]

CREDIT_NOTE_PATTERNS = [
    r"\bcredit[-_ ]?note\b", r"\bcr\s*note\b", r"\bcn[-_ ]?\d+", r"\bcredit memo\b",
]


def classify_transaction(
    description: str,
    taxable_amount: float,
    vat_amount: float,
    direction: str = "purchase",
    supplier_trn: Optional[str] = None,
    suggested_type: Optional[str] = None,
) -> Tuple[str, float, bool, str]:
    """Classify transaction into statutory VAT categories.
    
    Args:
        description: Line item or transaction text description.
        taxable_amount: Amount of taxable supply in AED.
        vat_amount: VAT charged/declared.
        direction: 'purchase' or 'sales'.
        supplier_trn: Supplier TRN.
        suggested_type: Untrusted hint from LLM.
        
    Returns:
        tuple (transaction_type, effective_rate, is_recoverable, reason)
    """
    clean_desc = (description or "").lower()

    # 1. Credit Notes (Negative amounts or credit note keyword markers)
    is_credit_note = taxable_amount < 0 or vat_amount < 0 or any(re.search(p, clean_desc) for p in CREDIT_NOTE_PATTERNS)
    if is_credit_note:
        if direction == "purchase":
            return (
                "credit_note_purchase",
                0.05,
                True,
                "Purchase credit note: reduces recoverable input tax in the current period (Articles 61 & 62).",
            )
        else:
            return (
                "credit_note_sales",
                0.05,
                False,
                "Sales credit note: reduces declared output tax in the current period (Articles 61 & 62).",
            )

    # 2. Reverse Charge Mechanism (Article 48)
    if any(re.search(p, clean_desc) for p in REVERSE_CHARGE_PATTERNS) or suggested_type == "reverse_charge":
        return (
            "reverse_charge",
            0.05,
            True,
            "Reverse charge mechanism (Article 48): Recipient accounts for 5% output tax and concurrent 5% recoverable input tax.",
        )

    # 3. Customs Goods Imports (Articles 48 & 49)
    if any(re.search(p, clean_desc) for p in IMPORT_PATTERNS) or suggested_type == "import":
        return (
            "import",
            0.05,
            True,
            "Import of goods: reciprocal accounting in Box 6 (Output VAT) and Box 10 (Recoverable Input VAT).",
        )

    # 4. Zero-rated supplies (Article 45)
    if any(re.search(p, clean_desc) for p in ZERO_RATED_PATTERNS) or (vat_amount == 0.0 and suggested_type == "zero_rated"):
        return (
            "zero_rated",
            0.0,
            True,
            "Zero-rated supply (Article 45): 0% output VAT with full input tax recovery eligibility.",
        )

    # 5. Exempt supplies (Article 46)
    if any(re.search(p, clean_desc) for p in EXEMPT_PATTERNS) or (vat_amount == 0.0 and suggested_type == "exempt"):
        return (
            "exempt",
            0.0,
            False,
            "Exempt supply (Article 46): 0% output VAT and zero input tax recoverability.",
        )

    # 6. Standard-rated (5%)
    # Verify standard rate math within tolerance
    if taxable_amount > 0:
        expected_vat = round(taxable_amount * 0.05, 2)
        diff = abs(round(vat_amount, 2) - expected_vat)
        if diff <= 0.05:
            return (
                "standard_rated",
                0.05,
                True,
                "Standard-rated supply (5%) under Federal Decree-Law No. 8 of 2017 Article 3.",
            )

    # 7. Discrepant or uncertain items -> special_review (NEEDS_HUMAN_REVIEW)
    return (
        "special_review",
        round(vat_amount / taxable_amount, 4) if taxable_amount > 0 else 0.0,
        False,
        "Ambiguous VAT rate or transaction character. Escalated to human tax review.",
    )
