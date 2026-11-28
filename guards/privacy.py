"""Local-first privacy and entity masking guard.

Anonymizes sensitive corporate and personal identifiers (TRN, IBAN, emails, names, passwords, verification codes)
before payloads are dispatched to external cloud LLM endpoints, application logs, or audit logs.
"""
import re
from typing import Tuple, Dict, Any, Union, List


def mask_trn(trn: str) -> str:
    """Mask a 15-digit TRN while preserving prefix and suffix for validation sanity."""
    clean = trn.strip()
    if len(clean) == 15 and clean.isdigit():
        return f"{clean[:3]}XXXXXXXXX{clean[-3:]}"
    return "[MASKED_TRN]"


def mask_iban(iban: str) -> str:
    """Mask a bank IBAN preserving country code and last 4 digits."""
    clean = iban.strip().replace(" ", "")
    if len(clean) >= 15:
        return f"{clean[:4]}XXXXXXXXXXXXXXXX{clean[-4:]}"
    return "[MASKED_IBAN]"


def mask_log_message(message: str) -> str:
    """Mask sensitive data in log messages (passwords, codes, TRNs, IBANs, long text)."""
    text = str(message)
    # Mask passwords
    text = re.sub(r'(?i)(password|pwd|secret)["\']?\s*[:=]\s*["\']?[^"\'\s,]+["\']?', r'\1=[MASKED_SECRET]', text)
    # Mask 6-digit codes
    text = re.sub(r'(?i)(code|totp|token)["\']?\s*[:=]\s*["\']?\d{6}["\']?', r'\1=[MASKED_CODE]', text)
    # Mask 15-digit TRNs
    text = re.sub(r'\b100\d{12}\b', lambda m: mask_trn(m.group(0)), text)
    # Mask UAE IBANs
    text = re.sub(r'\bAE\d{21}\b', lambda m: mask_iban(m.group(0)), text, flags=re.IGNORECASE)
    return text


def sanitize_audit_details(details: Union[Dict[str, Any], Any]) -> Dict[str, Any]:
    """Recursively strip or mask passwords, codes, and raw document text from audit details."""
    if not isinstance(details, dict):
        return details

    sensitive_keys = {
        "password",
        "pwd",
        "password_hash",
        "code",
        "verification_code",
        "reset_code",
        "totp_secret",
        "secret",
        "extracted_text",
        "raw_text",
        "raw_content",
    }

    sanitized: Dict[str, Any] = {}
    for k, v in details.items():
        k_lower = k.lower()
        if k_lower in sensitive_keys:
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, dict):
            sanitized[k] = sanitize_audit_details(v)
        elif isinstance(v, list):
            sanitized[k] = [sanitize_audit_details(item) if isinstance(item, dict) else item for item in v]
        elif isinstance(v, str):
            if k_lower in ("trn", "supplier_trn", "customer_trn"):
                sanitized[k] = mask_trn(v)
            elif k_lower in ("iban", "masked_iban"):
                sanitized[k] = mask_iban(v)
            else:
                sanitized[k] = mask_log_message(v)
        else:
            sanitized[k] = v

    return sanitized


def mask_payload(data: Union[str, Dict[str, Any]]) -> Tuple[Union[str, Dict[str, Any]], Dict[str, str]]:
    """Mask PII entities in a string or dictionary and return a replacement lookup mapping."""
    mapping: Dict[str, str] = {}

    if isinstance(data, str):
        text = data
        # 1. Mask 15-digit TRNs starting with 100
        trn_pattern = r"\b100\d{12}\b"
        for match in set(re.findall(trn_pattern, text)):
            masked = mask_trn(match)
            mapping[masked] = match
            text = text.replace(match, masked)

        # 2. Mask UAE IBANs (AE followed by 21 digits)
        iban_pattern = r"\bAE\d{21}\b"
        for match in set(re.findall(iban_pattern, text, re.IGNORECASE)):
            masked = mask_iban(match)
            mapping[masked] = match
            text = text.replace(match, masked)

        # 3. Mask Emails
        email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        for idx, match in enumerate(set(re.findall(email_pattern, text))):
            placeholder = f"[EMAIL_MASKED_{idx+1}]"
            mapping[placeholder] = match
            text = text.replace(match, placeholder)

        return text, mapping

    elif isinstance(data, dict):
        masked_dict = {}
        for k, v in data.items():
            if isinstance(v, (str, dict)):
                masked_val, sub_map = mask_payload(v)
                masked_dict[k] = masked_val
                mapping.update(sub_map)
            else:
                masked_dict[k] = v
        return masked_dict, mapping

    return data, mapping


def unmask_payload(text: str, mapping: Dict[str, str]) -> str:
    """Restore original values using the lookup mapping."""
    result = text
    for masked, original in mapping.items():
        result = result.replace(masked, original)
    return result
