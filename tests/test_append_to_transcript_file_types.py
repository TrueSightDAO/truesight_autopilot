"""Unit tests for append_to_transcript's file_type validation gate."""

from __future__ import annotations

import json

from app.tools.attachment_tools import append_to_transcript


def test_word_is_an_accepted_file_type():
    # Word must pass the gate (session_id required next, not file_type).
    result = json.loads(append_to_transcript("", "content", "file.docx", "Word"))
    assert result["reason"] != "file_type must be 'PDF', 'Image', or 'Word'"


def test_pdf_and_image_still_accepted():
    for file_type in ("PDF", "Image"):
        result = json.loads(append_to_transcript("", "content", "f", file_type))
        assert result["reason"] != "file_type must be 'PDF', 'Image', or 'Word'"


def test_invalid_file_type_rejected():
    result = json.loads(append_to_transcript("sess", "content", "f", "Spreadsheet"))
    assert result["status"] == "error"
    assert "PDF" in result["reason"] and "Word" in result["reason"]
