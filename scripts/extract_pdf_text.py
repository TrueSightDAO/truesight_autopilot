#!/usr/bin/env python3
"""Extract text from PDF files.

Primary: pymupdf (fitz) — pure Python, fast, no system deps.
Fallback: pdfminer.six — handles PDFs that pymupdf can't parse.

Usage:
    python3 scripts/extract_pdf_text.py <path_to_pdf>

Output:
    JSON with status, text content (per page), page count, and quality flags.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("extract_pdf_text")

MAX_PAGES = 100  # safety limit
MAX_CHARS_PER_PAGE = 50_000


def extract_with_pymupdf(path: str) -> dict:
    """Extract text using pymupdf (fitz)."""
    import fitz  # pymupdf

    doc = fitz.open(path)
    pages = []
    total_chars = 0
    scanned_flags = []

    for i, page in enumerate(doc):
        if i >= MAX_PAGES:
            break
        text = page.get_text()
        char_count = len(text)
        total_chars += char_count

        # Detect scanned/image-only pages (very little extractable text)
        is_scanned = char_count < 50
        scanned_flags.append(is_scanned)

        if char_count > MAX_CHARS_PER_PAGE:
            text = text[:MAX_CHARS_PER_PAGE] + "\n...[TRUNCATED]"

        pages.append({
            "page": i + 1,
            "char_count": char_count,
            "text": text.strip(),
            "is_scanned": is_scanned,
        })

    doc.close()

    scanned_ratio = sum(scanned_flags) / len(scanned_flags) if scanned_flags else 0

    return {
        "status": "success",
        "method": "pymupdf",
        "page_count": len(pages),
        "total_chars": total_chars,
        "scanned_ratio": round(scanned_ratio, 2),
        "likely_scanned_pdf": scanned_ratio > 0.8,
        "pages": pages,
    }


def extract_with_pdfminer(path: str) -> dict:
    """Fallback: extract text using pdfminer.six."""
    from pdfminer.high_level import extract_text
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        text = extract_text(path)
    except PDFSyntaxError as e:
        return {"status": "error", "message": f"PDF syntax error: {e}"}

    char_count = len(text)
    return {
        "status": "success",
        "method": "pdfminer",
        "page_count": 1,  # pdfminer doesn't give per-page easily
        "total_chars": char_count,
        "scanned_ratio": 0.0,
        "likely_scanned_pdf": char_count < 50,
        "pages": [{"page": 1, "char_count": char_count, "text": text.strip(), "is_scanned": char_count < 50}],
    }


def extract_pdf_text(path: str) -> dict:
    """Extract text from a PDF file. Tries pymupdf first, falls back to pdfminer."""
    p = Path(path)
    if not p.exists():
        return {"status": "error", "message": f"File not found: {path}"}
    if p.stat().st_size == 0:
        return {"status": "error", "message": "File is empty"}
    if p.stat().st_size > 100 * 1024 * 1024:
        return {"status": "error", "message": "File too large (>100 MB)"}

    # Try pymupdf first
    try:
        import fitz
        return extract_with_pymupdf(path)
    except ImportError:
        logger.warning("pymupdf not available, trying pdfminer...")
    except Exception as e:
        logger.warning(f"pymupdf failed: {e}, trying pdfminer...")

    # Fallback to pdfminer
    try:
        import pdfminer
        return extract_with_pdfminer(path)
    except ImportError:
        return {"status": "error", "message": "Neither pymupdf nor pdfminer available. Install one: pip install pymupdf"}
    except Exception as e:
        return {"status": "error", "message": f"pdfminer also failed: {e}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Usage: extract_pdf_text.py <path>"}))
        sys.exit(1)

    result = extract_pdf_text(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))
