"""Official FTA Tax Knowledge Base Package.

Grounds all UAE VAT recovery reasoning and rule explanations in official
Federal Tax Authority legislation, guides, and public clarifications.
"""
from knowledge.indexer import (
    KnowledgeChunk,
    KnowledgeIndex,
    get_global_index,
    extract_chunks_from_pdf,
    extract_chunks_from_text,
    index_uploaded_guide,
)
from knowledge.retriever import (
    search_tax_knowledge,
    get_rule_verification_statuses,
    parse_tax_period_dates,
)

__all__ = [
    "KnowledgeChunk",
    "KnowledgeIndex",
    "get_global_index",
    "extract_chunks_from_pdf",
    "extract_chunks_from_text",
    "index_uploaded_guide",
    "search_tax_knowledge",
    "get_rule_verification_statuses",
    "parse_tax_period_dates",
]
