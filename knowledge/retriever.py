"""Deterministic RAG Retriever for Official UAE Tax Guidance.

Uses rank_bm25 to query uploaded official FTA guides, laws, and regulations.
Enforces version awareness per tax period and deterministic grounding:
if no official text supports the query, returns an explicit escalation note
preventing hallucination.
"""
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import yaml
import re

from knowledge.indexer import (
    get_global_index,
    tokenize,
    KnowledgeChunk,
    extract_chunks_from_text,
)


def parse_tax_period_dates(period_code: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a tax period code (e.g., '2021-Q1', '2020-12', '2026-Q2') into (start_date, end_date)."""
    p = period_code.strip().upper()
    
    # Quarter pattern: YYYY-Q1..Q4
    qm = re.match(r"^(\d{4})[-/]?Q([1-4])$", p)
    if qm:
        year = int(qm.group(1))
        q = int(qm.group(2))
        q_starts = {1: f"{year}-01-01", 2: f"{year}-04-01", 3: f"{year}-07-01", 4: f"{year}-10-01"}
        q_ends = {1: f"{year}-03-31", 2: f"{year}-06-30", 3: f"{year}-09-30", 4: f"{year}-12-31"}
        return q_starts[q], q_ends[q]

    # Month pattern: YYYY-MM
    mm = re.match(r"^(\d{4})[-/](\d{2})$", p)
    if mm:
        year = int(mm.group(1))
        m = int(mm.group(2))
        import calendar
        _, last_day = calendar.monthrange(year, m)
        return f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last_day:02d}"

    # Year only: YYYY
    ym = re.match(r"^(\d{4})$", p)
    if ym:
        year = int(ym.group(1))
        return f"{year}-01-01", f"{year}-12-31"

    return None, None


def is_chunk_in_force_for_period(chunk: KnowledgeChunk, start_date: str, end_date: str) -> bool:
    """Check if the legislative chunk was in force during the specified tax period."""
    if not chunk.effective_from:
        return True

    # If document took effect AFTER the period ended, it was not in force during that period
    if chunk.effective_from > end_date:
        return False

    # If document expired or was superseded BEFORE the period started, it was no longer in force
    if chunk.effective_to and chunk.effective_to < start_date:
        return False

    return True


def search_tax_knowledge(
    query: str,
    tax_period: Optional[str] = None,
    top_k: int = 3,
    db_path: Optional[Path] = None,
    min_score: float = 0.5,
) -> Dict[str, Any]:
    """Retrieve top official FTA passages matching the query with version awareness.
    
    Returns structured results with exact document name and page citation.
    If no relevant official guidance is found, returns an explicit escalation notice.
    """
    index = get_global_index()
    if not index.chunks or index.bm25 is None:
        # Try initializing from database and disk if not yet loaded
        index.build_from_database_and_disk(db_path=db_path)

    if not index.chunks or index.bm25 is None:
        return {
            "found": False,
            "passages": [],
            "message": "No supporting official FTA guidance found; human review required. Please check with the FTA or a licensed tax advisor.",
            "version_consulted": None,
            "citation": "No official FTA guidance found — human review required",
        }

    q_tokens = tokenize(query)
    if not q_tokens:
        return {
            "found": False,
            "passages": [],
            "message": "Search query was empty or contained no searchable keywords.",
            "version_consulted": None,
            "citation": "No official FTA guidance found — human review required",
        }

    # BM25 scoring
    raw_scores = index.bm25.get_scores(q_tokens)

    # Version-aware adjustment and keyword overlap grounding
    period_start, period_end = (None, None)
    if tax_period:
        period_start, period_end = parse_tax_period_dates(tax_period)

    q_set = set(q_tokens)
    scored_chunks: List[Tuple[float, KnowledgeChunk, bool]] = []
    for raw_score, chunk in zip(raw_scores, index.chunks):
        chunk_token_set = set(chunk.tokens)
        matching_tokens = q_set.intersection(chunk_token_set)
        if not matching_tokens:
            continue

        overlap_ratio = len(matching_tokens) / len(q_set)
        # Combine BM25 term weighting with term overlap count
        effective_score = float(raw_score) + (overlap_ratio * 3.0) + (len(matching_tokens) * 0.5)

        in_force = True
        multiplier = 1.0
        if period_start and period_end:
            in_force = is_chunk_in_force_for_period(chunk, period_start, period_end)
            if in_force:
                # Boost guidance that was actively in force during the period
                multiplier = 1.5
            else:
                # If query is not specifically about future/transitional rules, down-weight out-of-period rules
                if "transitional" not in query.lower() and "expiry" not in query.lower() and "deadline" not in query.lower():
                    multiplier = 0.3

        final_score = effective_score * multiplier
        if final_score >= min_score:
            scored_chunks.append((final_score, chunk, in_force))

    scored_chunks.sort(key=lambda x: x[0], reverse=True)

    if not scored_chunks:
        version_note = None
        if tax_period:
            version_note = f"Evaluated for tax period {tax_period}"
        return {
            "found": False,
            "passages": [],
            "message": f"No supporting official FTA guidance found for '{query}'; human review required. Please check with the FTA or a licensed tax advisor.",
            "version_consulted": version_note,
            "citation": "No official FTA guidance found — human review required",
        }

    top_results = scored_chunks[:top_k]
    passages = []
    versions_used = []

    for score, chunk, in_force in top_results:
        eff_str = f"{chunk.effective_from or '2018-01-01'} to {chunk.effective_to or 'present'}"
        passages.append({
            "document_title": chunk.document_title,
            "page_number": chunk.page_number,
            "issuer": chunk.issuer,
            "version_date": chunk.version_date,
            "effective_period": eff_str,
            "in_force_for_period": in_force,
            "text": chunk.text,
            "score": round(score, 3),
            "citation": f"Source: {chunk.document_title}, page {chunk.page_number}",
        })
        v_desc = f"{chunk.document_title} (in force: {eff_str})"
        if v_desc not in versions_used:
            versions_used.append(v_desc)

    primary_citation = passages[0]["citation"]
    if tax_period:
        version_consulted = f"{'; '.join(versions_used)} [Applicable for tax period {tax_period}]"
    else:
        version_consulted = "; ".join(versions_used)

    return {
        "found": True,
        "passages": passages,
        "primary_citation": primary_citation,
        "version_consulted": version_consulted,
        "message": f"Found {len(passages)} supporting passage(s) from official guidance.",
    }


def get_rule_verification_statuses(
    db_path: Optional[Path] = None,
    rules_file_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Load statutory rules from config/tax_rules.yaml and verify against official knowledge passages.
    
    Rules backed by indexed passages from uploaded official documents are marked 'verified'.
    Rules lacking supporting uploaded guidance are marked 'to verify'.
    """
    rule_path = rules_file_path or Path("config/tax_rules.yaml")
    if not rule_path.exists():
        return []

    try:
        content = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
        rules_data = content.get("rules", [])
    except Exception:
        return []

    index = get_global_index()
    has_corpus = bool(index.chunks and index.bm25)

    verified_rules = []
    for r in rules_data:
        rule_id = r.get("id", "UNKNOWN_RULE")
        desc = r.get("description", "")
        official_src = r.get("official_source", "")
        eff_from = r.get("effective_from", "2018-01-01")
        eff_to = r.get("effective_to")

        # Determine verification status
        is_verified = False
        matching_citation = None

        if has_corpus:
            # Query index for rule's official source or key concepts
            q = f"{official_src} {desc[:80]}"
            res = search_tax_knowledge(query=q, min_score=1.2, top_k=1, db_path=db_path)
            if res.get("found") and res.get("passages"):
                is_verified = True
                matching_citation = res["passages"][0]["citation"]

        status = "verified" if is_verified else "to verify"
        status_label = "Verified ✓" if is_verified else "To verify"
        badge = "🟢 Verified" if is_verified else "🟠 To verify"

        # Check if this rule affects typical recovery claims
        affects_claim = rule_id in (
            "RULE_STANDARD_VAT_RATE",
            "RULE_ENTERTAINMENT_BLOCKED",
            "RULE_PERSONAL_MOTOR_VEHICLES_BLOCKED",
            "RULE_STATUTORY_LIMITATION_5_YEAR",
            "RULE_TRANSITIONAL_PRE_2022_EXPIRY",
            "RULE_TAX_INVOICE_REQUIREMENT",
        )

        verified_rules.append({
            "id": rule_id,
            "description": desc,
            "official_source": official_src,
            "effective_from": eff_from,
            "effective_to": eff_to or "Present",
            "status": status,
            "status_label": status_label,
            "badge": badge,
            "matching_citation": matching_citation,
            "affects_claim": affects_claim,
        })

    return verified_rules
