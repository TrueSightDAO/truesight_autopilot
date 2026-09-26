"""Unit tests for scripts/extract_docx_text.py and its attachment_tools wrapper."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "extract_docx_text.py"


def _make_docx(tmp_path, paragraphs=None, table_rows=None) -> Path:
    d = docx.Document()
    for p in paragraphs or []:
        d.add_paragraph(p)
    if table_rows:
        table = d.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, val in enumerate(row):
                table.cell(r, c).text = val
    path = tmp_path / "test.docx"
    d.save(str(path))
    return path


def _run(path: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), path], capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


def test_extracts_paragraphs_and_table(tmp_path):
    path = _make_docx(
        tmp_path,
        paragraphs=["Hello world.", "Second paragraph."],
        table_rows=[["Name", "Value"], ["Foo", "Bar"]],
    )
    result = _run(str(path))
    assert result["status"] == "success"
    assert result["paragraph_count"] == 2
    assert result["table_count"] == 1
    assert "Hello world." in result["text"]
    assert "Foo | Bar" in result["text"]


def test_missing_file_returns_error(tmp_path):
    result = _run(str(tmp_path / "nonexistent.docx"))
    assert result["status"] == "error"


def test_empty_file_returns_error(tmp_path):
    path = tmp_path / "empty.docx"
    path.write_bytes(b"")
    result = _run(str(path))
    assert result["status"] == "error"
    assert "empty" in result["message"].lower()


def test_not_a_zip_file_returns_clear_error(tmp_path):
    """A .docx extension on a non-OOXML file (e.g. legacy .doc) should give
    a clear, actionable error rather than a raw exception traceback."""
    path = tmp_path / "legacy.docx"
    path.write_bytes(b"not a real docx file, just plain bytes")
    result = _run(str(path))
    assert result["status"] == "error"
    msg = result["message"].lower()
    assert "legacy" in msg or "not a valid" in msg


def test_empty_docx_no_content(tmp_path):
    path = _make_docx(tmp_path, paragraphs=[])
    result = _run(str(path))
    assert result["status"] == "success"
    assert result["paragraph_count"] == 0
    assert result["text"] == ""


def test_attachment_tools_wrapper(tmp_path):
    from app.tools.attachment_tools import extract_docx_text

    path = _make_docx(tmp_path, paragraphs=["Wrapper test content."])
    result = json.loads(extract_docx_text(str(path)))
    assert result["status"] == "success"
    assert "Wrapper test content." in result["text"]


def test_attachment_tools_wrapper_missing_file():
    from app.tools.attachment_tools import extract_docx_text

    result = json.loads(extract_docx_text("/nonexistent/path.docx"))
    assert result["status"] == "error"
