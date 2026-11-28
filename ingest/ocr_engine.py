"""Local scanned document and image OCR engine using rapidocr-onnxruntime."""
from pathlib import Path
from typing import Dict, Any, List
import io

_ocr_instance = None


class OCRUnavailableError(RuntimeError):
    """Raised when the optional OCR engine is not installed on this server."""


def get_ocr_engine():
    """Lazy singleton getter for the local RapidOCR engine.

    The heavy OCR/OpenCV stack is only imported the first time a scanned
    document or photo is processed, so app start-up stays fast and the app
    still runs on hosts where OCR cannot be installed.
    """
    global _ocr_instance
    if _ocr_instance is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:  # ImportError, or cv2 system-library errors
            raise OCRUnavailableError(
                "Photo/scan reading is not available on this server. "
                "Please upload a text PDF, Excel or CSV file instead."
            ) from exc
        _ocr_instance = RapidOCR()
    return _ocr_instance


def _to_pil(image_path_or_bytes: Any):
    from PIL import Image
    if isinstance(image_path_or_bytes, (str, Path)):
        img = Image.open(str(Path(image_path_or_bytes).resolve()))
    elif isinstance(image_path_or_bytes, (bytes, bytearray)):
        img = Image.open(io.BytesIO(image_path_or_bytes))
    else:
        img = image_path_or_bytes
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)  # phone photos: respect rotation
    except Exception:
        pass
    return img.convert("RGB")


def _enhanced_copy(img):
    """Second-pass copy: grayscale, enlarged, contrast-stretched and sharpened."""
    from PIL import ImageFilter, ImageOps
    g = ImageOps.grayscale(img)
    if g.width < 1600:
        scale = 1600 / max(g.width, 1)
        g = g.resize((int(g.width * scale), int(g.height * scale)))
    g = ImageOps.autocontrast(g, cutoff=1)
    g = g.filter(ImageFilter.SHARPEN)
    return g.convert("RGB")


def _run_pass(engine, img) -> List[tuple]:
    import numpy as np
    result, _ = engine(np.array(img))
    out = []
    for item in result or []:
        if len(item) >= 3 and str(item[1]).strip():
            out.append((item[0], str(item[1]).strip(), float(item[2])))
    return out


_OCR_CACHE: Dict[str, Dict[str, Any]] = {}


def _cache_key(data: bytes, tag: str) -> str:
    import hashlib
    return tag + hashlib.sha1(data).hexdigest()


def _cache_put(key: str, value: Dict[str, Any]) -> None:
    if len(_OCR_CACHE) > 48:
        _OCR_CACHE.pop(next(iter(_OCR_CACHE)))
    _OCR_CACHE[key] = value


def extract_ocr_from_image(image_path_or_bytes: Any, double_pass: bool = True) -> Dict[str, Any]:
    """Cached wrapper: the same image bytes are only read once per server process."""
    if isinstance(image_path_or_bytes, (bytes, bytearray)):
        key = _cache_key(bytes(image_path_or_bytes), "img2" if double_pass else "img1")
        if key in _OCR_CACHE:
            return dict(_OCR_CACHE[key])
        res = _extract_ocr_from_image(image_path_or_bytes, double_pass)
        _cache_put(key, res)
        return dict(res)
    return _extract_ocr_from_image(image_path_or_bytes, double_pass)


def rows_from_boxes(boxes: List[tuple]) -> List[str]:
    """Rebuild reading-order rows from OCR word boxes (table cells come back one by one).

    Boxes whose vertical centres are within ~60% of a line height are put on the same
    row and sorted left to right, so 'Q1 2026 | 42000 | 51000' becomes one line again.
    """
    items = []
    for box, text in boxes:
        try:
            ys = [p[1] for p in box]; xs = [p[0] for p in box]
            items.append(((min(ys) + max(ys)) / 2, max(max(ys) - min(ys), 1.0), min(xs), str(text)))
        except Exception:
            continue
    if not items:
        return []
    items.sort(key=lambda r: r[0])
    heights = sorted(h for _, h, _, _ in items)
    tol = heights[len(heights) // 2] * 0.6
    rows: List[List[tuple]] = []
    for it in items:
        if rows and abs(it[0] - (sum(r[0] for r in rows[-1]) / len(rows[-1]))) <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return ["  ".join(t for *_rest, t in sorted(r, key=lambda r: r[2])) for r in rows]


def _extract_ocr_from_image(image_path_or_bytes: Any, double_pass: bool = True) -> Dict[str, Any]:
    """Run local OCR on an image (path, bytes or PIL image), in two passes.

    Pass 1 reads the original image; pass 2 reads an enhanced copy (grayscale,
    enlarged, sharpened). Lines from both passes are merged (duplicates removed,
    best confidence kept), which recovers text that either pass alone misses.

    Returns dict: text, lines, avg_confidence, boxes (from pass 1), passes.
    """
    engine = get_ocr_engine()
    img = _to_pil(image_path_or_bytes)
    first = _run_pass(engine, img)
    second: List[tuple] = []
    if double_pass:
        try:
            second = _run_pass(engine, _enhanced_copy(img))
        except Exception:
            second = []

    merged: Dict[str, tuple] = {}
    order: List[str] = []
    for _box, text, score in first + second:
        key = " ".join(text.lower().split())
        if key not in merged:
            order.append(key)
            merged[key] = (text, score)
        elif score > merged[key][1]:
            merged[key] = (text, score)

    if not order:
        return {"text": "", "lines": [], "avg_confidence": 0.0, "boxes": [], "passes": 2 if double_pass else 1}

    lines = [merged[k][0] for k in order]
    scores = [merged[k][1] for k in order]
    boxes = [(b, t) for b, t, _s in first]
    return {
        "text": "\n".join(lines),
        "lines": lines,
        "row_text": "\n".join(rows_from_boxes(boxes)),
        "avg_confidence": round(sum(scores) / len(scores), 3),
        "boxes": boxes,
        "passes": 2 if double_pass else 1,
    }


def extract_ocr_from_scanned_pdf(pdf_path: Path) -> Dict[str, Any]:
    """Extract text from scanned PDF pages by rendering them and running RapidOCR (2 passes)."""
    import pypdfium2 as pdfium

    try:
        key = _cache_key(Path(pdf_path).read_bytes(), "pdf")
        if key in _OCR_CACHE:
            return dict(_OCR_CACHE[key])
    except Exception:
        key = None

    pdf = pdfium.PdfDocument(str(pdf_path))
    all_lines: List[str] = []
    all_rows: List[str] = []
    all_scores: List[float] = []

    for i, page in enumerate(pdf):
        if i >= 6:  # keep big scans fast
            break
        pil_image = page.render(scale=2.0).to_pil()
        res = extract_ocr_from_image(pil_image)
        all_lines.extend(res["lines"])
        if res.get("row_text"):
            all_rows.append(res["row_text"])
        if res["lines"]:
            all_scores.append(res["avg_confidence"])

    consolidated = "\n".join(all_lines)
    overall_conf = round(sum(all_scores) / len(all_scores), 3) if all_scores else 0.0

    out = {
        "text": consolidated,
        "lines": all_lines,
        "row_text": "\n".join(all_rows),
        "avg_confidence": overall_conf,
    }
    if key:
        _cache_put(key, out)
    return out
