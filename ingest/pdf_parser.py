"""Digital PDF text and table extraction using pdfplumber."""
import io
from pathlib import Path
from typing import Dict, Any, List, Union
import pdfplumber


def extract_text_from_pdf(pdf_input: Union[Path, str, bytes]) -> Dict[str, Any]:
    """Extract raw text and detected tables from digital PDF bytes or file path.
    
    Returns:
        dict containing:
            - text: consolidated text string from all pages
            - page_texts: list of text per page
            - tables: list of extracted tabular rows
            - page_count: total pages
    """
    page_texts: List[str] = []
    tables: List[List[List[str]]] = []

    if isinstance(pdf_input, (bytes, bytearray)):
        pdf_file = io.BytesIO(pdf_input)
    else:
        pdf_file = Path(pdf_input).resolve()
        if not pdf_file.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_file}")

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            page_texts.append(text)
            page_tables = page.extract_tables() or []
            tables.extend(page_tables)

    consolidated_text = "\n\n--- Page Break ---\n\n".join(page_texts).strip()

    return {
        "text": consolidated_text,
        "page_texts": page_texts,
        "tables": tables,
        "page_count": len(page_texts),
    }


def extract_pdf_content(pdf_path: Path) -> Dict[str, Any]:
    """Extract raw text and detected tables from a digital PDF file."""
    return extract_text_from_pdf(pdf_path)
