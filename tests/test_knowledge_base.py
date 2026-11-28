"""Tests for Phase 6: Official FTA Knowledge Base (rank_bm25 RAG).

Validates:
1. Document chunking with page number and title metadata.
2. BM25 keyword search returns top passages with exact page citations.
3. Version awareness: correct statutory versions applied per tax period.
4. Ungrounded queries return explicit escalation notice without hallucination.
5. Rules loader cross-references config/tax_rules.yaml and tracks verification status.
"""
import pytest
from pathlib import Path
from datetime import date

from knowledge.indexer import (
    KnowledgeChunk,
    KnowledgeIndex,
    get_global_index,
    extract_chunks_from_text,
    tokenize,
    infer_document_metadata,
)
from knowledge.retriever import (
    search_tax_knowledge,
    get_rule_verification_statuses,
    parse_tax_period_dates,
    is_chunk_in_force_for_period,
)


@pytest.fixture(autouse=True)
def clean_global_index():
    """Ensure a clean knowledge index for each test."""
    idx = get_global_index()
    idx.clear()
    yield
    idx.clear()


def test_tokenize():
    tokens = tokenize("Federal Decree-Law No. 8 of 2017 on Value Added Tax (VAT) in UAE!")
    assert "federal" in tokens
    assert "decree" in tokens
    assert "2017" in tokens
    assert "vat" in tokens
    assert "uae" in tokens


def test_extract_chunks_from_text_with_pages():
    sample_text = """
    --- Page 1 ---
    Federal Decree-Law No. (8) of 2017 on Value Added Tax
    Article 53: Non-Recoverable Input Tax
    Input tax shall not be recovered on goods or services acquired for entertainment purposes
    provided to any person not employed by the taxable person.
    
    --- Page 2 ---
    Article 54: Recoverable Input Tax
    A taxable person may recover input tax paid or incurred on taxable expenses provided that
    they receive and retain a valid Tax Invoice showing the supplier's TRN and amount of VAT.
    """
    chunks = extract_chunks_from_text(
        content=sample_text,
        document_title="Federal Decree-Law No. 8 of 2017",
        filename="decree_law_8_2017.txt",
    )
    assert len(chunks) >= 2
    assert chunks[0].page_number == 1
    assert "entertainment" in chunks[0].text.lower()
    assert chunks[0].document_title == "Federal Decree-Law No. 8 of 2017"
    assert chunks[0].issuer == "Ministry of Finance"

    assert chunks[1].page_number == 2
    assert "invoice" in chunks[1].text.lower()


def test_bm25_search_returns_cited_passages():
    sample_guide = """
    --- Page 1 ---
    Federal Tax Authority VAT User Guide
    Input tax recovery requires a complete Tax Invoice.
    The invoice must state the phrase Tax Invoice, the date, supplier name and 15-digit TRN.

    --- Page 2 ---
    Article 53 Entertainment Exclusion Rules
    Entertainment provided to customers, prospective clients, or government officials
    cannot be claimed as recoverable input VAT. This is strictly non-recoverable under UAE Law.
    """
    idx = get_global_index()
    chunks = extract_chunks_from_text(
        content=sample_guide,
        document_title="FTA VAT User Guide",
        filename="fta_vat_guide.txt",
    )
    idx.add_chunks(chunks)

    # Query entertainment
    res = search_tax_knowledge(query="entertainment provided to customers non-recoverable")
    assert res["found"] is True
    assert len(res["passages"]) >= 1
    top = res["passages"][0]
    assert top["page_number"] == 2
    assert "Source: FTA VAT User Guide, page 2" in res["primary_citation"]
    assert "non-recoverable" in top["text"].lower()


def test_version_awareness_historical_vs_amendment():
    """Verify that historical tax periods prioritize versions in force at that time."""
    idx = get_global_index()

    # Document 1: 2017 Decree-Law (In force 2018 to 2024)
    chunks_2017 = extract_chunks_from_text(
        content="""
        --- Page 10 ---
        Federal Decree-Law No. 8 of 2017
        Statutory time limit for input tax claims: 5 years from the end of the tax period.
        """,
        document_title="Federal Decree-Law No. 8 of 2017",
        filename="dl_8_2017.txt",
        metadata_override={
            "effective_from": "2018-01-01",
            "effective_to": "2024-12-31",
            "issuer": "Ministry of Finance",
        },
    )

    # Document 2: 2025 Decree-Law 17 (In force from 2025)
    chunks_2025 = extract_chunks_from_text(
        content="""
        --- Page 4 ---
        Decree-Law No. 17 of 2025 Transitional Rule
        Transitional provisions: Excess input tax credits accrued before 1 January 2022
        must be claimed by 31 December 2026, or they shall be permanently forfeited.
        """,
        document_title="Federal Decree-Law No. 17 of 2025",
        filename="dl_17_2025.txt",
        metadata_override={
            "effective_from": "2025-01-01",
            "effective_to": "2026-12-31",
            "issuer": "Ministry of Finance",
        },
    )

    idx.add_chunks(chunks_2017 + chunks_2025)

    # Test period date parser
    p_start, p_end = parse_tax_period_dates("2021-Q1")
    assert p_start == "2021-01-01"
    assert p_end == "2021-03-31"

    # Search for period 2021-Q1 with general rule
    res_hist = search_tax_knowledge(query="statutory time limit input tax claims", tax_period="2021-Q1")
    assert res_hist["found"] is True
    top_hist = res_hist["passages"][0]
    assert top_hist["document_title"] == "Federal Decree-Law No. 8 of 2017"
    assert "2021-Q1" in res_hist["version_consulted"]

    # Search for transitional rule in 2026
    res_trans = search_tax_knowledge(query="transitional rule excess input tax pre 2022", tax_period="2026-Q1")
    assert res_trans["found"] is True
    top_trans = res_trans["passages"][0]
    assert top_trans["document_title"] == "Federal Decree-Law No. 17 of 2025"
    assert "2026-Q1" in res_trans["version_consulted"]


def test_ungrounded_query_escalation():
    """Verify that unsupported queries return explicit notice without hallucination."""
    idx = get_global_index()
    chunks = extract_chunks_from_text(
        content="Standard VAT rate is 5% under Article 3 of Decree-Law No. 8 of 2017.",
        document_title="Federal Decree-Law No. 8 of 2017",
    )
    idx.add_chunks(chunks)

    # Completely unrelated question
    res = search_tax_knowledge(query="cryptocurrency mining capital gains tax offshore")
    assert res["found"] is False
    assert len(res["passages"]) == 0
    assert "No supporting official FTA guidance found" in res["message"]
    assert "human review required" in res["message"]


def test_empty_knowledge_index_escalation():
    """Verify that an empty index returns the explicit escalation notice."""
    idx = get_global_index()
    idx.clear()

    res = search_tax_knowledge(query="What is the VAT rate on medical supplies?")
    assert res["found"] is False
    assert "No supporting official FTA guidance found" in res["message"]


def test_get_rule_verification_statuses_loader():
    """Verify tax rules are loaded from config/tax_rules.yaml with verification badges."""
    rules = get_rule_verification_statuses()
    assert len(rules) >= 5

    # Check structure
    r0 = rules[0]
    assert "id" in r0
    assert "description" in r0
    assert "official_source" in r0
    assert r0["status"] in ("verified", "to verify")
    assert "badge" in r0
    assert "affects_claim" in r0

    # Index official decree-law text to verify standard rate
    idx = get_global_index()
    sample = """
    Federal Decree-Law No. 8 of 2017 on Value Added Tax, Article 3
    Standard VAT rate of 5% applies to taxable supplies of goods and services.
    """
    idx.add_chunks(extract_chunks_from_text(sample, "Federal Decree-Law No. 8 of 2017"))

    verified_rules = get_rule_verification_statuses()
    std_rule = next((r for r in verified_rules if r["id"] == "RULE_STANDARD_VAT_RATE"), None)
    assert std_rule is not None
    assert std_rule["status"] == "verified"
    assert "🟢" in std_rule["badge"]


def test_chat_agent_rules_view():
    """Verify that show_panel('rules') returns structured statutory rules data."""
    from agent.chat_agent import show_panel
    res = show_panel(view="rules", business_id=1)
    assert res["view"] == "rules"
    assert "rules_count" in res
    assert res["rules_count"] >= 5
    assert "verified_count" in res
    assert "data" in res
