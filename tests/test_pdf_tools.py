"""Unit tests for the PDF generation tool."""

from __future__ import annotations

import base64
import json
import os

import pytest

reportlab = pytest.importorskip("reportlab")

from app.tools import pdf_tools  # noqa: E402 — must follow the reportlab importorskip guard above


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


def test_diacritics_roundtrip(tmp_path):
    """Latin Extended-A + Pali dot-unders must survive as real glyphs.

    Regression: reportlab's built-in Helvetica is WinAnsi-only, so "Ānāpāna"
    silently rendered as "InIpIna" (wrong glyphs). We now embed DejaVu Sans.
    """
    fitz = pytest.importorskip("fitz")
    out_path = tmp_path / "diacritics.pdf"
    out = json.loads(
        pdf_tools.generate_pdf(
            content="# Ānāpāna & Vipassanā\n\nvedanā, mettā, anicca, São Paulo, ṭhāna\n",
            title="Ānāpāna and Vipassanā",
            output_path=str(out_path),
        )
    )
    assert out["status"] == "ok"

    doc = fitz.open(str(out_path))
    text = "\n".join(page.get_text() for page in doc)
    for token in ("Ānāpāna", "Vipassanā", "vedanā", "mettā", "São"):
        assert token in text, f"{token!r} missing from rendered text"
    # The old corruption must be gone.
    assert "InIpIna" not in text


def test_unicode_font_is_embedded(tmp_path):
    """The DejaVu font (not just base-14 Helvetica) must be embedded."""
    fitz = pytest.importorskip("fitz")
    out_path = tmp_path / "font.pdf"
    out = json.loads(
        pdf_tools.generate_pdf(content="Ānāpāna", title="T", output_path=str(out_path))
    )
    assert out["status"] == "ok"
    doc = fitz.open(str(out_path))
    fonts = {f[3] for page in doc for f in page.get_fonts()}
    assert any("DejaVu" in name for name in fonts), fonts
