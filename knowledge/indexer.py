"""Official FTA Knowledge Base Indexer.

Extracts, chunks, and indexes official UAE VAT documents (Federal Tax Authority
guides, VAT Law, Executive Regulations, Public Clarifications) using rank_bm25.
Grounds all tax rule explanations strictly in official texts.
"""
from dataclasses import dataclass, field
from datetime import datetime, date
import re
import os
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from rank_bm25 import BM25Okapi
import pdfplumber

from database.connection import get_db_connection


@dataclass
class KnowledgeChunk:
    chunk_id: str
    document_title: str
    filename: str
    page_number: int
    issuer: str
    version_date: Optional[str] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    text: str = ""
    tokens: List[str] = field(default_factory=list)


def tokenize(text: str) -> List[str]:
    """Simple alphanumeric lowercase tokenizer for BM25."""
    return re.findall(r"\b[a-zA-Z0-9_\u0600-\u06FF]{2,}\b", text.lower())


def infer_document_metadata(title: str, text_sample: str = "") -> Dict[str, Any]:
    """Infer issuer, version, and effective date range from document title and text."""
    title_lower = title.lower()
    text_sample_lower = text_sample.lower()

    issuer = "Federal Tax Authority"
    if "decree-law" in title_lower or "decree law" in title_lower or "ministry of finance" in text_sample_lower:
        issuer = "Ministry of Finance"

    # Default statutory timeline for UAE VAT
    version_date = "2018-01-01"
    effective_from = "2018-01-01"
    effective_to = None

    if "2017" in title_lower or "decree-law no. 8" in title_lower or "decree-law no. (8)" in title_lower:
        version_date = "2017-08-23"
        effective_from = "2018-01-01"
        effective_to = None
    elif "decree-law no. 16" in title_lower or "decree-law no. (16)" in title_lower or "16 of 2024" in title_lower:
        version_date = "2024-09-30"
        effective_from = "2024-10-01"
        effective_to = None
    elif "decree-law no. 17" in title_lower or "decree-law no. (17)" in title_lower or "17 of 2025" in title_lower:
        version_date = "2025-01-01"
        effective_from = "2025-01-01"
        effective_to = "2026-12-31"  # Transitional deadline cut-off
    elif "2025" in title_lower:
        version_date = "2025-01-01"
        effective_from = "2025-01-01"
        effective_to = None

    return {
        "issuer": issuer,
        "version_date": version_date,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def chunk_text(text: str, chunk_size_words: int = 400, overlap_words: int = 50) -> List[str]:
    """Split text into overlapping word chunks."""
    words = text.split()
    if not words:
        return []
    if len(words) <= chunk_size_words:
        return [text.strip()]

    chunks = []
    step = max(1, chunk_size_words - overlap_words)
    for i in range(0, len(words), step):
        window = words[i: i + chunk_size_words]
        chunks.append(" ".join(window).strip())
        if i + chunk_size_words >= len(words):
            break
    return chunks


def extract_chunks_from_pdf(
    file_path: Path,
    document_title: Optional[str] = None,
    metadata_override: Optional[Dict[str, Any]] = None,
) -> List[KnowledgeChunk]:
    """Extract page-by-page text from PDF and produce chunks with metadata."""
    chunks: List[KnowledgeChunk] = []
    filename = file_path.name
    doc_title = document_title or filename.replace(".pdf", "").replace("_", " ").title()

    sample_text = ""
    pages_text: List[Tuple[int, str]] = []

    try:
        with pdfplumber.open(file_path) as pdf:
            for page_idx, page in enumerate(pdf.pages, start=1):
                p_text = page.extract_text() or ""
                if p_text.strip():
                    pages_text.append((page_idx, p_text.strip()))
                    if not sample_text:
                        sample_text = p_text[:1000]
    except Exception:
        # If pdfplumber fails, return empty
        return []

    inferred = infer_document_metadata(doc_title, sample_text)
    if metadata_override:
        inferred.update(metadata_override)

    for page_num, page_content in pages_text:
        text_slices = chunk_text(page_content, chunk_size_words=350, overlap_words=40)
        for s_idx, slice_text in enumerate(text_slices):
            chunk_id = f"{filename}_p{page_num}_c{s_idx}"
            tokens = tokenize(slice_text)
            if tokens:
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_title=doc_title,
                        filename=filename,
                        page_number=page_num,
                        issuer=inferred["issuer"],
                        version_date=inferred["version_date"],
                        effective_from=inferred["effective_from"],
                        effective_to=inferred["effective_to"],
                        text=slice_text,
                        tokens=tokens,
                    )
                )

    return chunks


def extract_chunks_from_text(
    content: str,
    document_title: str,
    filename: str = "document.txt",
    metadata_override: Optional[Dict[str, Any]] = None,
) -> List[KnowledgeChunk]:
    """Create chunks from a raw text/markdown string (supporting page separators)."""
    chunks: List[KnowledgeChunk] = []
    inferred = infer_document_metadata(document_title, content[:1000])
    if metadata_override:
        inferred.update(metadata_override)

    # Check for page markers e.g. "--- Page 1 ---" or "\f"
    pages = re.split(r"(?:---|===)\s*Page\s*(\d+)\s*(?:---|===)|\f", content, flags=re.IGNORECASE)
    
    if len(pages) > 1:
        cur_page = 1
        i = 0
        while i < len(pages):
            segment = pages[i]
            if segment and segment.strip().isdigit():
                cur_page = int(segment.strip())
                i += 1
                if i < len(pages):
                    segment = pages[i]
                else:
                    break
            if segment and segment.strip():
                for s_idx, sl in enumerate(chunk_text(segment.strip())):
                    tokens = tokenize(sl)
                    if tokens:
                        chunks.append(
                            KnowledgeChunk(
                                chunk_id=f"{filename}_p{cur_page}_c{s_idx}",
                                document_title=document_title,
                                filename=filename,
                                page_number=cur_page,
                                issuer=inferred["issuer"],
                                version_date=inferred["version_date"],
                                effective_from=inferred["effective_from"],
                                effective_to=inferred["effective_to"],
                                text=sl,
                                tokens=tokens,
                            )
                        )
            i += 1
    else:
        # Single page document
        for s_idx, sl in enumerate(chunk_text(content)):
            tokens = tokenize(sl)
            if tokens:
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=f"{filename}_p1_c{s_idx}",
                        document_title=document_title,
                        filename=filename,
                        page_number=1,
                        issuer=inferred["issuer"],
                        version_date=inferred["version_date"],
                        effective_from=inferred["effective_from"],
                        effective_to=inferred["effective_to"],
                        text=sl,
                        tokens=tokens,
                    )
                )

    return chunks


class KnowledgeIndex:
    """Thread-safe in-memory BM25 index over official FTA guidance."""

    def __init__(self):
        self._lock = threading.RLock()
        self.chunks: List[KnowledgeChunk] = []
        self.bm25: Optional[BM25Okapi] = None
        self._initialized = False

    def clear(self):
        with self._lock:
            self.chunks = []
            self.bm25 = None
            self._initialized = False

    def add_chunks(self, new_chunks: List[KnowledgeChunk]):
        with self._lock:
            if not new_chunks:
                return
            existing_ids = {c.chunk_id for c in self.chunks}
            for c in new_chunks:
                if c.chunk_id not in existing_ids:
                    self.chunks.append(c)
                    existing_ids.add(c.chunk_id)

            if self.chunks:
                corpus = [c.tokens for c in self.chunks]
                self.bm25 = BM25Okapi(corpus)
                self._initialized = True

    def build_from_database_and_disk(self, db_path: Optional[Path] = None, data_dir: Optional[Path] = None):
        """Load and index all uploaded FTA guides from SQLite and disk."""
        with self._lock:
            self.chunks = []
            
            # 1. Check disk directories
            candidates: List[Path] = []
            search_dirs = [
                Path("data/knowledge"),
                Path("data/uploads/fta_guide"),
                Path("data/uploads"),
            ]
            if data_dir:
                search_dirs.insert(0, Path(data_dir))

            for d in search_dirs:
                if d.exists() and d.is_dir():
                    candidates.extend(d.glob("*.pdf"))
                    candidates.extend(d.glob("*.txt"))

            # 2. Check SQLite uploaded_documents
            try:
                conn = get_db_connection(db_path=db_path)
                with conn:
                    rows = conn.execute(
                        "SELECT original_filename, storage_path FROM uploaded_documents WHERE document_type = 'fta_guide';"
                    ).fetchall()
                    for r in rows:
                        sp = Path(r["storage_path"])
                        if sp.exists() and sp not in candidates:
                            candidates.append(sp)
                conn.close()
            except Exception:
                pass

            # Extract chunks
            for p in candidates:
                if p.suffix.lower() == ".pdf":
                    c = extract_chunks_from_pdf(p)
                    self.chunks.extend(c)
                elif p.suffix.lower() in (".txt", ".md"):
                    try:
                        text = p.read_text(encoding="utf-8")
                        c = extract_chunks_from_text(text, document_title=p.stem.replace("_", " ").title(), filename=p.name)
                        self.chunks.extend(c)
                    except Exception:
                        pass

            if self.chunks:
                corpus = [c.tokens for c in self.chunks]
                self.bm25 = BM25Okapi(corpus)
            else:
                self.bm25 = None
            self._initialized = True


# Global index singleton
_GLOBAL_INDEX = KnowledgeIndex()


def get_global_index() -> KnowledgeIndex:
    return _GLOBAL_INDEX


def index_uploaded_guide(
    file_path: Path,
    original_filename: str,
    metadata_override: Optional[Dict[str, Any]] = None,
) -> int:
    """Index a newly uploaded official FTA guide. Returns number of chunks created."""
    idx = get_global_index()
    if str(file_path).endswith(".pdf"):
        chunks = extract_chunks_from_pdf(
            file_path,
            document_title=original_filename.replace(".pdf", "").replace("_", " ").title(),
            metadata_override=metadata_override,
        )
    else:
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            chunks = extract_chunks_from_text(
                content,
                document_title=original_filename.replace("_", " ").title(),
                filename=original_filename,
                metadata_override=metadata_override,
            )
        except Exception:
            chunks = []

    if chunks:
        idx.add_chunks(chunks)
    return len(chunks)
