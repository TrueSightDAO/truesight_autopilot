"""Attachment processing tools for the autopilot.

Provides tools to extract text from PDFs and images, and persist
results to the session transcript.

These wrap the CLI scripts in scripts/ so the agent can call them
directly as tools.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("autopilot.tools.attachment_tools")

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def _run_script(script_name: str, *args: str) -> dict:
    """Run a helper script and parse its JSON output."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {"status": "error", "message": f"Script not found: {script_path}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"Script exited {result.returncode}: {result.stderr[:500]}",
            }
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"Script output was not valid JSON: {e}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Script timed out after 120 seconds."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to run script: {e}"}


def extract_pdf_text(path: str) -> str:
    """Extract text from a PDF file using pymupdf (fallback to pdfminer).

    If the PDF is detected as scanned (image-only), the result includes
    a suggest_ocr flag so the agent can run OCR on the file.

    Args:
        path: Full path to the PDF file.

    Returns:
        JSON string with extracted text per page.
    """
    if not path or not os.path.isfile(path):
        return json.dumps({"status": "error", "reason": f"File not found: {path}"})
    result = _run_script("extract_pdf_text.py", path)

    # If the PDF is likely scanned (image-only), add OCR suggestion
    if isinstance(result, dict) and result.get("suggest_ocr"):
        result["ocr_suggestion"] = (
            "This PDF appears to be scanned (image-only). Try running ocr_image on the file for text extraction."
        )

    return json.dumps(result, indent=2)


def ocr_image(path: str, lang: str = "eng") -> str:
    """Run OCR on an image file using Tesseract.

    Args:
        path: Full path to the image file.
        lang: Tesseract language code (default: eng).

    Returns:
        JSON string with extracted text and confidence.
    """
    if not path or not os.path.isfile(path):
        return json.dumps({"status": "error", "reason": f"File not found: {path}"})
    result = _run_script("ocr_image.py", path, lang)
    return json.dumps(result, indent=2)


def append_to_transcript(
    session_id: str,
    content: str,
    filename: str,
    file_type: str,
    ocr_text: str = "",
    grok_description: str = "",
) -> str:
    """Append extracted attachment content to the session transcript.

    Args:
        session_id: Session hash/ID.
        content: Main extracted text content.
        filename: Original filename of the attachment.
        file_type: "PDF" or "Image".
        ocr_text: OCR-extracted text (for images).
        grok_description: Grok vision description (for images).

    Returns:
        JSON string with status and transcript URL.
    """
    if not session_id or not content or not filename:
        return json.dumps(
            {
                "status": "error",
                "reason": "session_id, content, and filename are required",
            }
        )
    if file_type not in ("PDF", "Image"):
        return json.dumps(
            {"status": "error", "reason": "file_type must be 'PDF' or 'Image'"}
        )

    result = _run_script(
        "append_to_transcript.py",
        "--session-id",
        session_id,
        "--content",
        content,
        "--filename",
        filename,
        "--type",
        file_type,
        "--ocr-text",
        ocr_text,
        "--grok-description",
        grok_description,
    )
    return json.dumps(result, indent=2)


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="extract_pdf_text",
        description="Extract text from a PDF file using pymupdf (fallback to pdfminer). Returns per-page text content. Use this when a governor sends a PDF attachment.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the PDF file on disk.",
                }
            },
            "required": ["path"],
        },
        handler=lambda args, ctx: extract_pdf_text(args.get("path", "")),
    ),
    ToolSpec(
        name="ocr_image",
        description="Run OCR on an image file using Tesseract. Extracts visible text from photos, screenshots, and scanned documents. Use this when a governor sends an image attachment that contains text.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Full path to the image file on disk.",
                },
                "lang": {
                    "type": "string",
                    "description": "Tesseract language code (default: eng).",
                    "default": "eng",
                },
            },
            "required": ["path"],
        },
        handler=lambda args, ctx: ocr_image(
            args.get("path", ""), args.get("lang", "eng")
        ),
    ),
    ToolSpec(
        name="append_to_transcript",
        description="Append extracted attachment content (PDF text or OCR result) to the session transcript for cross-session recall. Call this after extracting content from a file so the governor can reference it later.",
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session hash/ID to attach the content to.",
                },
                "content": {
                    "type": "string",
                    "description": "Main extracted text content from the file.",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename of the attachment.",
                },
                "file_type": {
                    "type": "string",
                    "enum": ["PDF", "Image"],
                    "description": "Type of file: PDF or Image.",
                },
                "ocr_text": {
                    "type": "string",
                    "description": "OCR-extracted text (for images only).",
                    "default": "",
                },
                "grok_description": {
                    "type": "string",
                    "description": "Grok vision description (for images only).",
                    "default": "",
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
