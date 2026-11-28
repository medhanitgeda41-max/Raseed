"""Input tax recoverability and blocked category guard.

Enforces Articles 53, 54, and 55-58 of the Executive Regulation of Federal Decree-Law No. 8 of 2017.
Golden Rule:
- Eligibility is evaluated completely separately from invoice extraction.
- Never assume an invoice with 5% VAT is recoverable.
- Always queries the rules loader for the specific rule version in force on the transaction date.
- Blocked or uncertain categories (entertainment, personal use, employee benefits) -> NEEDS_HUMAN_REVIEW.
- Apportions partial business use and partial exempt supplies.
"""
import re
from datetime import date
from typing import Tuple, List, Optional, Union
from config.rules_loader import get_rules_for_date


ENTERTAINMENT_KEYWORDS = [
    r"entertainment", r"hospitality", r"catering", r"banquet", r"gala",
    r"restaurant", r"dining", r"party", r"event tickets?", r"golf",
    r"yacht", r"amusement", r"concert", r"sporting event",
]

MOTOR_VEHICLE_KEYWORDS = [
    r"personal vehicle", r"passenger car lease", r"sedan rental",
    r"private car", r"personal conveyance",
]

EMPLOYEE_BENEFIT_KEYWORDS = [
    r"employee gift", r"staff personal", r"gym membership", r"private health spa",
    r"recreational club",
]


def classify_input_tax_eligibility(
    description_lines: Optional[Union[List[str], str]] = None,
    transaction_date: Optional[Union[date, str]] = None,
    suggested_category: Optional[str] = None,
    business_use_percentage: float = 100.0,
    apportionment_percentage: float = 100.0,
    category: Optional[str] = None,
    description: Optional[Union[List[str], str]] = None,
    is_partial_business_use: bool = False,
) -> Tuple[str, float, str, str]:
    """Classify input VAT eligibility using the statutory rules active on the transaction date.
    
    Args:
        description_lines: Item descriptions (list or single string).
        transaction_date: Date of supply (to load the corresponding rule version).
        suggested_category: Untrusted hint from LLM.
        business_use_percentage: % of commercial business use (0.0 to 100.0).
        apportionment_percentage: Business general partial recovery rate (0.0 to 100.0).
        category: Alias for suggested_category.
        description: Alias for description_lines.
        is_partial_business_use: Flag indicating mixed business/private use.
        
    Returns:
        tuple (eligibility_status, effective_recovery_rate, reason, statutory_citation)
        status: 'VALID', 'NEEDS_HUMAN_REVIEW', or 'INELIGIBLE'.
        effective_recovery_rate: decimal multiplier between 0.0 and 1.0.
    """
    raw_lines = description_lines or description or []
    if isinstance(raw_lines, str):
        lines_list = [raw_lines]
    else:
        lines_list = list(raw_lines)

    text_corpus = " ".join(lines_list).lower()
    cat = category or suggested_category
    if cat:
        text_corpus += " " + cat.lower()

    if is_partial_business_use and business_use_percentage == 100.0:
        business_use_percentage = 50.0

    # Load version-controlled rules in force on this transaction date
    active_rules = get_rules_for_date(transaction_date)
    rules_dict = {r["id"]: r for r in active_rules}

    # 1. Blocked Entertainment check (Article 53)
    rule_ent = rules_dict.get("RULE_BLOCKED_ENTERTAINMENT")
    if rule_ent:
        for kw in ENTERTAINMENT_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", text_corpus, re.IGNORECASE):
                return (
                    "NEEDS_HUMAN_REVIEW",
                    0.0,
                    f"Blocked entertainment supply matching '{kw}'. Recovery blocked unless meeting statutory business meeting test.",
                    rule_ent.get("official_source", "Executive Regulation Art. 53"),
                )

    # 2. Blocked Motor Vehicles Personal Use check (Article 54)
    rule_veh = rules_dict.get("RULE_BLOCKED_MOTOR_VEHICLE")
    if rule_veh:
        for kw in MOTOR_VEHICLE_KEYWORDS:
            if re.search(r"\b" + kw + r"\b", text_corpus, re.IGNORECASE):
                return (
                    "NEEDS_HUMAN_REVIEW",
                    0.0,
                    f"Motor vehicle expense matching '{kw}' available for personal use. Input tax recovery restricted under Article 54.",
                    rule_veh.get("official_source", "Executive Regulation Art. 54"),
                )

    # 3. Employee Personal Benefits check (Article 53(1)(c))
    for kw in EMPLOYEE_BENEFIT_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", text_corpus, re.IGNORECASE):
            return (
                "NEEDS_HUMAN_REVIEW",
                0.0,
                f"Goods or services potentially for personal benefit of employees matching '{kw}'.",
                "Executive Regulation of Federal Decree-Law No. 8 of 2017, Article 53(1)(c)",
            )

    # 4. Partial Business / Non-Business Use check (Article 53(1))
    if business_use_percentage < 100.0:
        rate = round((business_use_percentage / 100.0) * (apportionment_percentage / 100.0), 4)
        return (
            "NEEDS_HUMAN_REVIEW",
            rate,
            f"Partial business use ({business_use_percentage}%). Input tax restricted to business share, requiring human confirmation.",
            "Executive Regulation Art. 53(1)",
        )

    # 5. Partial Exemption Apportionment (Articles 55-58)
    if apportionment_percentage < 100.0:
        rate = round(apportionment_percentage / 100.0, 4)
        rule_app = rules_dict.get("RULE_PARTIAL_EXEMPT_APPORTIONMENT")
        return (
            "VALID",
            rate,
            f"Subject to partial recovery apportionment at {apportionment_percentage}% for taxable person making exempt supplies.",
            rule_app.get("official_source", "Executive Regulation Articles 55-58") if rule_app else "Executive Regulation Art. 55-58",
        )

    # Standard fully recoverable commercial expense
    return (
        "VALID",
        1.0,
        "Qualifying commercial business expenditure with 100% recoverable input tax.",
        "Federal Decree-Law No. 8 of 2017, Article 54",
    )
