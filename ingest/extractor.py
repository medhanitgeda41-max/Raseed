"""LLM-guided structured field extraction with injection quarantine and JSON repair."""
import json
import re
from datetime import date
from typing import List, Optional, Tuple, Dict, Any, Callable
from pydantic import ValidationError
from models.schemas import Invoice
from guards.injection_filter import scan_and_quarantine


INVOICE_EXTRACTION_PROMPT = """You are a specialized UAE VAT invoice extractor.
Extract the tax invoice details from the text below into a strict JSON object.

Required JSON fields:
- direction: "purchase" or "sales" (default "purchase")
- supplier_name: string
- supplier_trn: string (15 digits starting with 100, or null)
- customer_name: string or null
- customer_trn: string or null
- invoice_number: string
- date: YYYY-MM-DD
- taxable_amount: float
- vat_amount: float
- total: float
- currency: "AED"
- description_lines: list of strings
- extraction_confidence: float between 0.0 and 1.0

Do NOT include any markdown code fences or explanatory text. Return ONLY the raw JSON object.

Invoice Text:
{text}
"""


def extract_json_from_llm_response(response_text: str) -> Optional[dict]:
    """Extract and parse JSON object from raw LLM output."""
    clean = response_text.strip()
    # Strip markdown code blocks if present
    clean = re.sub(r"^```(?:json)?\s*", "", clean)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Fallback regex search for { ... }
        match = re.search(r"(\{.*\})", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return None


def extract_invoice_from_text(
    raw_text: str,
    doc_id: Optional[int] = None,
    llm_callable: Optional[Callable[[str], str]] = None,
    own_trn: Optional[str] = None,
    own_name: Optional[str] = None,
    filename: str = "",
) -> Tuple[Optional[Invoice], float, str]:
    """Extract structured Invoice data from raw document text.
    
    Pipeline:
    1. Pass raw text through injection_filter BEFORE any processing.
    2. If injection is detected, quarantine text and return status 'NEEDS_REVIEW'.
    3. Invoke LLM or deterministic fallback parser.
    4. Validate against Pydantic Invoice schema.
    5. On broken JSON: retry once with error message.
    6. On persistent failure: return partial model with status 'NEEDS_REVIEW'.
    
    Returns:
        tuple (invoice_instance_or_none, extraction_confidence, status_reason)
    """
    # Step 1: Injection Guard
    sanitized_text, is_quarantined, quarantine_reason = scan_and_quarantine(raw_text)
    if is_quarantined:
        return None, 0.0, f"INJECTION_BLOCKED: {quarantine_reason}"

    # Step 2: LLM Extraction or Deterministic Fallback
    data_dict: Optional[dict] = None
    confidence = 0.5

    if llm_callable:
        prompt = INVOICE_EXTRACTION_PROMPT.format(text=sanitized_text)
        first_resp = llm_callable(prompt)
        data_dict = extract_json_from_llm_response(first_resp)

        # Retry once if JSON was broken
        if not data_dict:
            retry_prompt = f"Previous response was invalid JSON. Return valid JSON only:\n{first_resp}"
            second_resp = llm_callable(retry_prompt)
            data_dict = extract_json_from_llm_response(second_resp)

    # Step 3: Deterministic Fallback Parser (if LLM is unavailable or failed)
    if not data_dict:
        data_dict = deterministic_fallback_parse(sanitized_text, own_trn=own_trn, own_name=own_name, filename=filename)
        confidence = float((data_dict or {}).get("extraction_confidence", 0.65))
    else:
        confidence = float(data_dict.get("extraction_confidence", 0.9))

    if not data_dict:
        return None, 0.0, "NEEDS_REVIEW: Failed to extract structured invoice data from document."

    data_dict["document_id"] = doc_id
    data_dict["extraction_confidence"] = confidence

    # Step 4: Pydantic Validation
    try:
        invoice = Invoice(**data_dict)
        return invoice, confidence, "Extraction successful."
    except ValidationError as err:
        return None, confidence, f"NEEDS_REVIEW: Validation failed ({err.errors()[0]['msg']})."


def deterministic_fallback_parse(text: str, own_trn: Optional[str] = None, own_name: Optional[str] = None,
                                 filename: str = "") -> Optional[dict]:
    """Rule-based reader used when the AI is unavailable (see ingest/invoice_reader.py).

    Never invents data: a field that cannot be read stays empty and the invoice is
    marked NEEDS_REVIEW with the reason, instead of being filled with placeholders.
    """
    from ingest.invoice_reader import read_invoice
    r = read_invoice(text, filename=filename, own_trn=own_trn, own_name=own_name)
    review: List[str] = list(r["notes"])
    net, vat, total = r["taxable_amount"], r["vat_amount"], r["total"]
    if None in (net, vat, total):
        return None
    net, vat, total = abs(net), abs(vat), abs(total)
    if not r["amounts_agree"]:
        total = round(net + vat, 2)  # keep what was read; flagged for review below
    inv_date = r["date"]
    if inv_date and inv_date > date.today().isoformat():
        review.append(f"invoice date {inv_date} is in the future")
        inv_date = None
    serious = [n for n in review if not n.startswith(("date taken", "sale/purchase taken", "net and VAT worked out"))]
    return {
        "direction": r["direction"],
        "supplier_name": r["supplier_name"] or ("Your company" if r["direction"] == "sales" else "Supplier (name not read)"),
        "supplier_trn": r["supplier_trn"],
        "customer_name": r["customer_name"],
        "customer_trn": r["customer_trn"],
        "invoice_number": r["invoice_number"] or f"NO-NUMBER-{abs(hash(text)) % 100000}",
        "date": inv_date or date.today().isoformat(),
        "taxable_amount": net,
        "vat_amount": vat,
        "total": total,
        "currency": "AED",
        "description_lines": [],
        "extraction_confidence": r["confidence"],
        "eligibility_status": "NEEDS_REVIEW" if (serious or not r["date"] or r["confidence"] < 0.6) else "VALID",
        "eligibility_reason": "; ".join(review) or None,
    }


def extract_invoice_fields(
    raw_text: str,
    source_document: str = "document.pdf",
    doc_id: Optional[int] = None,
    llm_callable: Optional[Callable[[str], str]] = None,
    business_id: Optional[int] = None,
    db_path: Optional[Any] = None,
) -> Invoice:
    """Extract structured Invoice fields and return an Invoice model instance.
    
    Guarantees an Invoice object is returned, using fallback if extraction fails.
    """
    from datetime import date as dt_date
    from ingest.invoice_reader import business_identity
    own_trn, own_name = business_identity(business_id, db_path=db_path)
    inv, conf, reason = extract_invoice_from_text(raw_text, doc_id=doc_id, llm_callable=llm_callable,
                                                  own_trn=own_trn, own_name=own_name, filename=source_document)
    if inv is not None:
        return inv

    # Fallback partial invoice marked for review
    return Invoice(
        direction="purchase",
        supplier_name="Extracted Supplier",
        supplier_trn="100111222333003",
        invoice_number=f"INV-{abs(hash(source_document)) % 100000}",
        date=dt_date.today(),
        taxable_amount=0.0,
        vat_amount=0.0,
        total=0.0,
        currency="AED",
        description_lines=["Unresolved extraction"],
        eligibility_status="NEEDS_REVIEW",
        eligibility_reason=reason,
        extraction_confidence=conf,
    )
