"""Show where each problem is *inside* a stored file, with red boxes.

- CSV / Excel: the table is drawn as HTML; problem rows are tinted and the exact
  cells get a red box.
- Text PDF: each page is rendered (pdfplumber) and the matching words get a red box.
- Scanned PDF / photo (PNG, JPG): words are located with OCR boxes and outlined in red.

Locations come from the `find` hints recorded by `tools.compute_tools.document_problems`.
"""
from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RED = (220, 38, 38)


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _cell_matches(cell: Any, find: str) -> bool:
    if cell is None:
        return False
    c, f = _norm(cell), _norm(find)
    if not f or c in ("", "nan", "none"):
        return False
    if f in c:
        return True
    try:  # numbers: 9400 == 9400.0 == "9,400.00"
        return abs(float(str(cell).replace(",", "")) - float(f.replace(",", ""))) < 0.005
    except ValueError:
        return False


# ------------------------------------------------------------------ tables

def render_table(file_bytes: bytes, filename: str, items: List[Dict[str, Any]], max_rows: int = 400,
                 only_problem_rows: bool = False) -> Dict[str, Any]:
    import pandas as pd
    if Path(filename).suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    else:
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding="latin1")
    df = df.fillna("")

    bad_cells: Dict[Tuple[int, int], List[int]] = {}
    located: Dict[int, List[str]] = {}
    for idx, it in enumerate(items):
        finds = it.get("find") or []
        if not finds:
            continue
        primary = finds[0]
        rows = [r for r in range(len(df)) if any(_cell_matches(v, primary) for v in df.iloc[r].tolist())]
        for r in rows:
            for c in range(len(df.columns)):
                if any(_cell_matches(df.iat[r, c], f) for f in finds):
                    bad_cells.setdefault((r, c), []).append(idx)
            located.setdefault(idx, []).append(f"row {r + 2}")  # +2: header line + 1-based

    bad_rows = {r for (r, _c) in bad_cells}
    if only_problem_rows:
        shown_rows = sorted(bad_rows)
    else:
        shown_rows = list(range(min(len(df), max_rows)))
        for r in sorted(bad_rows):
            if r not in shown_rows:
                shown_rows.append(r)

    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for r in shown_rows:
        tds = []
        for c in range(len(df.columns)):
            val = html.escape(str(df.iat[r, c]))
            if (r, c) in bad_cells:
                nums = ",".join(str(i + 1) for i in bad_cells[(r, c)])
                tds.append(f"<td class='rs-bad' title='Problem {nums}'>{val}<span class='rs-tag'>{nums}</span></td>")
            else:
                tds.append(f"<td>{val}</td>")
        cls = " class='rs-badrow'" if r in bad_rows else ""
        body.append(f"<tr{cls}><td class='rs-rn'>{r + 2}</td>{''.join(tds)}</tr>")
    table_html = (
        "<style>"
        ".rs-doc{border-collapse:collapse;font-size:12.5px;width:100%}"
        ".rs-doc th{position:sticky;top:0;background:#F1F5F9;border:1px solid #CBD5E1;padding:5px 7px;text-align:left;white-space:nowrap}"
        ".rs-doc td{border:1px solid #E2E8F0;padding:4px 7px;white-space:nowrap}"
        ".rs-doc tr.rs-badrow td{background:#FEF2F2}"
        ".rs-doc td.rs-bad{outline:3px solid #DC2626;outline-offset:-3px;background:#FEE2E2 !important;font-weight:600;color:#7F1D1D;position:relative}"
        ".rs-doc .rs-tag{display:inline-block;margin-left:6px;background:#DC2626;color:#fff;border-radius:9px;padding:0 6px;font-size:10.5px}"
        ".rs-doc td.rs-rn{color:#94A3B8;background:#F8FAFC;text-align:right}"
        "</style>"
        f"<table class='rs-doc'><thead><tr><th>#</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )
    return {"kind": "table", "html": table_html, "located": located, "rows": len(df)}


# ------------------------------------------------------------------ images (OCR boxes)

def _ocr_boxes(img) -> List[Tuple[List[Tuple[float, float]], str]]:
    try:
        import numpy as np
        from ingest.ocr_engine import get_ocr_engine
        result, _ = get_ocr_engine()(np.array(img.convert("RGB")))
        return [([tuple(p) for p in box], text) for box, text, _score in (result or [])]
    except Exception:
        return []


def _draw_ocr_matches(img, items: List[Dict[str, Any]], page_label: str, located: Dict[int, List[str]]):
    from PIL import ImageDraw
    boxes = _ocr_boxes(img)
    if not boxes:
        return img, False
    draw = ImageDraw.Draw(img, "RGBA")
    for idx, it in enumerate(items):
        for f in it.get("find") or []:
            fn = _norm(f)
            if len(fn) < 3:
                continue
            for pts, text in boxes:
                tn = _norm(text)
                if fn in tn or (len(tn) >= 4 and tn in fn) or _cell_matches(text, f):
                    draw.polygon(pts, outline=RED + (255,), fill=RED + (40,), width=4)
                    x, y = pts[0]
                    draw.rectangle([x, y - 16, x + 22, y], fill=RED + (255,))
                    draw.text((x + 6, y - 15), str(idx + 1), fill=(255, 255, 255, 255))
                    located.setdefault(idx, []).append(page_label)
    return img, True


def render_image(file_bytes: bytes, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    from PIL import Image
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    located: Dict[int, List[str]] = {}
    img, ok = _draw_ocr_matches(img, items, "on the image", located)
    return {"kind": "pages", "images": [img], "located": located,
            "note": None if ok else "Text positions could not be read from this image on this server."}


# ------------------------------------------------------------------ PDFs

def render_pdf(file_bytes: bytes, items: List[Dict[str, Any]], max_pages: int = 6) -> Dict[str, Any]:
    import pdfplumber
    images, located, notes = [], {}, []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pno, page in enumerate(pdf.pages[:max_pages], 1):
            words_text = (page.extract_text() or "").strip()
            im = page.to_image(resolution=110)
            if len(words_text) < 20:
                # scanned page: use OCR boxes on the rendered image
                pil = im.original.convert("RGB")
                pil, ok = _draw_ocr_matches(pil, items, f"page {pno}", located)
                if not ok:
                    notes.append(f"Page {pno} is a scan; text positions could not be read on this server.")
                images.append(pil)
                continue
            rects = []
            for idx, it in enumerate(items):
                for f in it.get("find") or []:
                    if len(str(f).strip()) < 3:
                        continue
                    try:
                        hits = page.search(str(f), regex=False, case=False)
                    except Exception:
                        hits = []
                    for h in hits:
                        rects.append((h["x0"] - 3, h["top"] - 3, h["x1"] + 3, h["bottom"] + 3))
                        located.setdefault(idx, []).append(f"page {pno}")
            if rects:
                im.draw_rects(rects, stroke=RED, stroke_width=4, fill=RED + (35,))
            images.append(im.annotated.convert("RGB"))
    return {"kind": "pages", "images": images, "located": located, "note": " ".join(notes) or None}


def render_document_with_problems(file_bytes: bytes, filename: str, items: List[Dict[str, Any]],
                                  only_problem_rows: bool = False) -> Dict[str, Any]:
    """Pick the right renderer for the file type."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".csv", ".xlsx", ".xls"):
        return render_table(file_bytes, filename, items, only_problem_rows=only_problem_rows)
    if suffix == ".pdf":
        return render_pdf(file_bytes, items)
    if suffix in (".png", ".jpg", ".jpeg"):
        return render_image(file_bytes, items)
    return {"kind": "none", "located": {}, "note": "This file type cannot be previewed."}
