"""Statutory deadline engine for excess recoverable input tax (VAT credits).

Implements Article 74(3) of Federal Decree-Law No. 8 of 2017 as amended by
Federal Decree-Law No. 16 of 2025 (effective 1 Jan 2026) and the transitional
rule under the Tax Procedures Law as amended by Federal Decree-Law No. 17 of 2025.
"""
from datetime import date
from typing import Optional
from models.schemas import Deadline


def add_years(d: date, years: int) -> date:
    """Add years to date handling Feb 29 leap years safely."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Handles Feb 29 on non-leap years
        return d.replace(month=2, day=28, year=d.year + years)


def calculate_credit_expiry(
    period_code: str,
    period_end_date: date,
    current_date: Optional[date] = None,
) -> Deadline:
    """Compute statutory 5-year expiry and transitional relief deadline for VAT credit.
    
    Statutory Rules:
    1. Standard 5-Year Rule: Expiry = end of the tax period in which the credit arose + 5 years.
    2. Transitional Rule (Decree-Law 17/2025): If the 5-year period expired before 1 Jan 2026
       OR expires within one year of that date (on or before 31 Dec 2026), a refund application
       can be submitted until 31 December 2026.
    
    Status thresholds:
    - EXPIRED: days_remaining < 0
    - URGENT: 0 <= days_remaining < 60
    - DUE_SOON: 60 <= days_remaining < 180
    - OK: days_remaining >= 180
    """
    now = current_date or date.today()
    standard_expiry = add_years(period_end_date, 5)

    transitional_threshold = date(2026, 12, 31)
    is_transitional = False

    # Check if standard 5-year expired before 1 Jan 2026 or on/before 31 Dec 2026
    if standard_expiry <= transitional_threshold:
        effective_expiry = transitional_threshold
        is_transitional = True
    else:
        effective_expiry = standard_expiry

    days_remaining = (effective_expiry - now).days

    if days_remaining < 0:
        status = "EXPIRED"
    elif days_remaining < 60:
        status = "URGENT"
    elif days_remaining < 180:
        status = "DUE_SOON"
    else:
        status = "OK"

    return Deadline(
        period_code=period_code,
        period_end_date=period_end_date,
        standard_expiry_date=standard_expiry,
        effective_expiry_date=effective_expiry,
        is_transitional=is_transitional,
        days_remaining=days_remaining,
        status=status,
    )
