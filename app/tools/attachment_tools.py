"""Attachment processing tools for the autopilot.

Provides tools to extract text from PDFs, run OCR on images, and persist
extracted content to the session transcript.

Each tool wraps the corresponding script in ``scripts/`` so the LLM can
discover and call them through the tool registry.
"""
from __future__ import annotations

import json as _json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..tool_registry import ToolSpec

logger = logging.getLogger("autopilot.attachment_tools")

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _run_script(script_name: str, *args: str) -> dict[str, Any]:
    """Run a script from ``scripts/`` and return its JSON output."""
    script_path = _SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {"status": "error", "message": f"Script not found: {script_path}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), *args],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {"status": "error", "message": result.stderr.strip() or result.stdout.strip()}
        return _json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"{script_name} timed out after 120s"}
    except _json.JSONDecodeError as e:
        return {"status": "error", "message": f"Failed to parse {script_name} output: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"{script_name} failed: {e}"}


def extract_pdf_text(file_path: str) -> str:
    """Extract text from a PDF file.

    Uses pymupdf (fitz) with a fallback to pdfminer.six. Returns a JSON
    string with status, per-page text, page count, and quality flags.

    Args:
        file_path: Full path to the PDF file on disk.

    Returns:
        JSON-encoded result dict.
    """
    result = _run_script("extract_pdf_text.py", file_path)
    return _json.dumps(result, indent=2, ensure_ascii=False)


def ocr_image(file_path: str, lang: str = "eng") -> str:
    """Run OCR on an image file to extract text.

    Preprocesses the image (grayscale, contrast, sharpen, binarize) before
    running Tesseract OCR. Returns a JSON string with extracted text,
    confidence score, and quality assessment.

    Args:
        file_path: Full path to the image file on disk.
        lang: Tesseract language code (default: eng).

    Returns:
        JSON-encoded result dict.
    """
    args = [file_path]
    if lang != "eng":
        args.append(lang)
    result = _run_script("ocr_image.py", *args)
    return _json.dumps(result, indent=2, ensure_ascii=False)


def append_to_transcript(
    session_id: str,
    content: str,
    filename: str,
    file_type: str,
    ocr_text: str = "",
    grok_description: str = "",
) -> str:
    """Append extracted attachment content to the session transcript.

    Reads the current session transcript from the
    ``truesight_autopilot_transcript`` repo, appends a structured section
    with the extracted text, and commits/pushes the change.

    Args:
        session_id: Session hash/ID (e.g. from the X-Session-Id header).
        content: Main extracted text content (PDF text or Grok description).
        filename: Original filename of the attachment.
        file_type: "PDF" or "Image".
        ocr_text: OCR-extracted text (for images).
        grok_description: Grok vision description (for images).

    Returns:
        JSON-encoded result dict with status and transcript URL.
    """
    cmd = [
        "--session-id", session_id,
        "--content", content,
        "--filename", filename,
        "--type", file_type,
    ]
    if ocr_text:
        cmd += ["--ocr-text", ocr_text]
    if grok_description:
        cmd += ["--grok-description", grok_description]

    result = _run_script("append_to_transcript.py", *cmd)
    return _json.dumps(result, indent=2, ensure_ascii=False)


# ── capability manifest entries ───────────────────────────────────────────

TOOL_SPECS = [
    ToolSpec(
        name="extract_pdf_text",
        description="Extract text from a PDF file on disk. Uses pymupdf (fitz) with pdfminer fallback. Returns per-page text, page count, and quality flags (e.g. whether the PDF is a scanned image).",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Full path to the PDF file on the server filesystem.",
                },
            },
            "required": ["file_path"],
        },
        handler=lambda args, ctx: extract_pdf_text(args.get("file_path", "")),
    ),
    ToolSpec(
        name="ocr_image",
        description="Run OCR on an image file to extract text. Preprocesses the image (grayscale, contrast, sharpen, binarize) before Tesseract OCR. Returns extracted text, confidence score, and quality assessment.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Full path to the image file on the server filesystem.",
                },
                "lang": {
                    "type": "string",
                    "description": "Tesseract language code (default: 'eng'). Use 'por' for Portuguese, 'spa' for Spanish.",
                },
            },
            "required": ["file_path"],
        },
        handler=lambda args, ctx: ocr_image(
            args.get("file_path", ""),
            args.get("lang", "eng"),
        ),
    ),
    ToolSpec(
        name="append_to_transcript",
        description="Append extracted attachment content (PDF text, OCR result, Grok description) to the session transcript in the truesight_autopilot_transcript repo. Call this AFTER processing an attachment to persist the extracted data.",
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session hash/ID (e.g. from X-Session-Id header, or the session_id in the context).",
                },
                "content": {
                    "type": "string",
                    "description": "Main extracted text content (PDF text or Grok description).",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename of the attachment.",
                },
                "file_type": {
                    "type": "string",
                    "enum": ["PDF", "Image"],
                    "description": "Type of file: 'PDF' or 'Image'.",
                },
                "ocr_text": {
                    "type": "string",
                    "description": "OCR-extracted text (for images). Optional.",
                },
                "grok_description": {
                    "type": "string",
                    "description": "Grok vision description (for images). Optional.",
                },
            },
            "required": ["session_id", "content", "filename", "file_type"],
        },
        handler=lambda args, ctx: append_to_transcript(
            session_id=args.get("session_id", ""),
            content=args.get("content", ""),
            filename=args.get("filename", ""),
            file_type=args.get("file_type", ""),
            ocr_text=args.get("ocr_text", ""),
            grok_description=args.get("grok_description", ""),
        ),
    ),
]
