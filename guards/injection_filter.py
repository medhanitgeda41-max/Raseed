"""Prompt injection detection and text quarantine guard.

Golden Rule: Pure Python heuristics and deterministic pattern matching. Never relies on the LLM.
Scans chat messages and all extracted document text BEFORE any cognitive processing.
"""
import re
from typing import Tuple, List

# High-risk prompt injection and jailbreak signatures
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"system\s+override",
    r"you\s+are\s+now\s+(an?\s+)?(unrestricted|evil|dan|new)",
    r"developer\s+mode\s+enabled",
    r"jailbreak",
    r"approve\s+(this\s+)?refund(\s+immediately)?",
    r"pre[-_]?approved",
    r"auto[-_]?claim",
    r"bypass\s+(all\s+)?checks?",
    r"ignore\s+vat\s+rules?",
    r"set\s+amount\s+to",
    r"grant\s+full\s+refund",
    r"<\s*script[^>]*>",
    r"\[\s*system\s*\]",
    r"\{\{\s*system\s*\}\}",
    r"role\s*:\s*system",
    r"instruction\s*:\s*approve",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

QUARANTINE_REPLACEMENT = "[QUARANTINED TEXT]"


def scan_and_quarantine(text: str) -> Tuple[str, bool, str]:
    """Scan input text for potential prompt injection or policy bypass attempts.
    
    Args:
        text: Raw user chat message or text extracted from documents.
        
    Returns:
        tuple (sanitized_text, is_quarantined, reason)
        If malicious patterns are detected, returns (QUARANTINE_REPLACEMENT, True, reason).
        Otherwise returns (text, False, "").
    """
    if not text or not isinstance(text, str):
        return text or "", False, ""

    matched_reasons: List[str] = []

    for pattern in COMPILED_PATTERNS:
        match = pattern.search(text)
        if match:
            matched_reasons.append(f"Matched injection pattern: '{match.group(0)}'")

    if matched_reasons:
        reason_str = "; ".join(matched_reasons)
        return QUARANTINE_REPLACEMENT, True, reason_str

    return text, False, ""


def contains_injection(text: str) -> bool:
    """Convenience boolean check for injection attempts."""
    _, is_quarantined, _ = scan_and_quarantine(text)
    return is_quarantined
