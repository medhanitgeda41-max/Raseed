"""File upload security validator for Raseed.

Enforces:
- Max 20 MB per file
- Max 200 uploaded files per company
- Real magic bytes / signature validation (CSV, XLSX, XLS, PDF, PNG, JPG)
- Strict rejection of archives (ZIP, RAR, 7Z, TAR, GZ) unless valid XLSX
- Strict rejection of password-protected / encrypted PDFs
"""
import io
import os
import zipfile
from pathlib import Path
from typing import Optional, Tuple

import filetype
import pdfplumber

from database.connection import get_db_connection


MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_FILES_PER_COMPANY = 200

# Archive magic byte signatures
ARCHIVE_SIGNATURES = [
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"\x1f\x8b", "Gzip archive"),
    (b"BZh", "Bzip2 archive"),
    (b"\xfd7zXZ\x00", "XZ archive"),
]


def check_pdf_encrypted(file_bytes: bytes) -> bool:
    """Check if PDF is password-protected or encrypted."""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            # Check docinfo or access pages to see if encrypted
            if getattr(pdf, "is_encrypted", False):
                return True
            # Try reading first page
            if len(pdf.pages) > 0:
                _ = pdf.pages[0].extract_text()
        return False
    except Exception as e:
        err = str(e).lower()
        if "password" in err or "encrypt" in err or "decrypt" in err:
            return True
        return False


def validate_uploaded_file_security(
    file_bytes: bytes,
    filename: str,
    business_id: int,
    db_path: Optional[Path] = None,
) -> Tuple[bool, str, str]:
    """Validate file security before storage or processing.
    
    Returns (is_valid, message, detected_mime_type).
    """
    clean_name = str(filename).strip()
    ext = Path(clean_name).suffix.lower()

    # 1. Check file size (max 20 MB)
    if len(file_bytes) == 0:
        return False, "Uploaded file is empty (0 bytes).", ""
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        size_mb = len(file_bytes) / (1024 * 1024)
        return False, f"File exceeds maximum allowed size of 20 MB (received {size_mb:.1f} MB).", ""

    # 2. Check company upload quota (max 200 files)
    try:
        conn = get_db_connection(db_path=db_path, readonly=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM uploaded_documents WHERE business_id = ?;",
            (business_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if row and row["cnt"] >= MAX_FILES_PER_COMPANY:
            return False, f"Company file storage limit reached (max {MAX_FILES_PER_COMPANY} files per company).", ""
    except Exception:
        pass

    # 3. Check for archive signatures (disallow raw archives)
    for sig, name in ARCHIVE_SIGNATURES:
        if file_bytes.startswith(sig):
            return False, f"Archive format ({name}) is not supported. Please upload individual PDF or Excel files.", ""

    # 4. Content inspection by file type
    # Check PDF
    if ext == ".pdf" or file_bytes.startswith(b"%PDF-"):
        if not file_bytes.startswith(b"%PDF-"):
            return False, "File does not match authentic PDF format signatures.", ""
        if check_pdf_encrypted(file_bytes):
            return False, "Password-protected or encrypted PDFs are not supported. Please provide an unencrypted PDF.", ""
        return True, "Valid PDF document.", "application/pdf"

    # Check XLSX
    if ext == ".xlsx":
        if not file_bytes.startswith(b"PK\x03\x04"):
            return False, "File does not match authentic Excel XLSX format.", ""
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                namelist = z.namelist()
                if not any(n.startswith("xl/") or n == "[Content_Types].xml" for n in namelist):
                    return False, "Invalid XLSX container: missing workbook structure.", ""
            return True, "Valid Excel XLSX document.", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        except Exception:
            return False, "Corrupted or invalid XLSX archive file.", ""

    # Check XLS (OLE2 compound)
    if ext == ".xls":
        if file_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return True, "Valid Excel XLS document.", "application/vnd.ms-excel"
        return False, "File does not match authentic Excel XLS format.", ""

    # Check PNG
    if ext == ".png" or file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return True, "Valid PNG image.", "image/png"
        return False, "File does not match authentic PNG format.", ""

    # Check JPG / JPEG
    if ext in (".jpg", ".jpeg") or file_bytes.startswith(b"\xff\xd8\xff"):
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return True, "Valid JPEG image.", "image/jpeg"
        return False, "File does not match authentic JPEG format.", ""

    # Check CSV / TXT
    if ext in (".csv", ".txt"):
        # Disallow binary null bytes
        if b"\x00" in file_bytes[:4096]:
            return False, "Binary or executable content detected in CSV/text file.", ""
        try:
            # Try decoding utf-8 or latin-1
            file_bytes[:8192].decode("utf-8")
        except UnicodeDecodeError:
            try:
                file_bytes[:8192].decode("latin-1")
            except Exception:
                return False, "Invalid text encoding in CSV file.", ""
        return True, "Valid CSV text document.", "text/csv"

    # Unknown or unsupported file type
    kind = filetype.guess(file_bytes)
    if kind:
        if kind.extension in ("zip", "tar", "rar", "gz", "7z"):
            return False, f"Archive format ({kind.extension}) is not supported.", ""
        if kind.extension in ("exe", "dll", "elf", "so", "bin"):
            return False, "Executable or binary formats are strictly prohibited.", ""

    return False, f"Unsupported file type '{ext}'. Allowed types: PDF, XLSX, XLS, CSV, PNG, JPG.", ""
