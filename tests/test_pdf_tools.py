"""Unit tests for the PDF generation tool."""

from __future__ import annotations

import base64
import json
import os

import pytest

reportlab = pytest.importorskip("reportlab")

from app.tools import pdf_tools


def test_empty_content_returns_error():
    out = json.loads(pdf_tools.generate_pdf(""))
    assert out["status"] == "error"


def test_simple_markdown_renders_to_pdf(tmp_path):
    out_path = tmp_path / "test.pdf"
    out = json.loads(
        pdf_tools.generate_pdf(
            content="# Hello\n\nThis is **bold** and *italic*.\n\n- bullet one\n- bullet two",
            title="Test Doc",
            output_path=str(out_path),
        )
    )
    assert out["status"] == "ok"
    assert out["byte_count"] > 0
    assert out["output_path"] == str(out_path)
    assert os.path.exists(out_path)

    pdf_bytes = base64.b64decode(out["pdf_base64"])
    assert pdf_bytes.startswith(b"%PDF-")  # PDF magic number


def test_output_to_temp_when_no_path(tmp_path):
    out = json.loads(pdf_tools.generate_pdf(content="Hello world.", title="Untitled"))
    assert out["status"] == "ok"
    path = out["output_path"]
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read(5) == b"%PDF-"
    # Cleanup
    os.unlink(path)
