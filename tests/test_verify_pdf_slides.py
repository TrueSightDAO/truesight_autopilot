"""Unit tests for scripts/verify_pdf_slides.py — the slide-deck overlap checker."""

import subprocess
import sys

import fitz
import pytest

sys.path.insert(0, "scripts")
from verify_pdf_slides import verify_pdf  # noqa: E402


@pytest.fixture()
def good_deck(tmp_path):
    """A minimal clean 16:9 deck: title above a content panel, no overlaps."""
    p = tmp_path / "good.pdf"
    doc = fitz.open()
    page = doc.new_page(width=960, height=540)
    # header band
    page.draw_rect(fitz.Rect(0, 0, 960, 110), color=None, fill=(0.85, 0.46, 0.1))
    page.insert_text((40, 60), "Header title", fontsize=20)
    # content panel well below the header
    page.draw_rect(fitz.Rect(40, 150, 900, 500), color=None, fill=(0.98, 0.95, 0.89))
    page.insert_text((60, 180), "Body text inside the panel", fontsize=14)
    doc.save(str(p))
    doc.close()
    return str(p)


@pytest.fixture()
def bad_panel_deck(tmp_path):
    """A deck where a content panel starts inside the header band (the real bug)."""
    p = tmp_path / "bad.pdf"
    doc = fitz.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((40, 40), "Title under the header", fontsize=20)
    page.draw_rect(fitz.Rect(0, 30, 960, 220), color=None, fill=(0.98, 0.95, 0.89))
    doc.save(str(p))
    doc.close()
    return str(p)


def test_good_deck_passes(good_deck):
    assert verify_pdf(good_deck) == []


def test_panel_into_header_caught(bad_panel_deck):
    problems = verify_pdf(bad_panel_deck)
    assert any("panel×header" in p for p in problems)


def test_cli_exit_codes(good_deck, bad_panel_deck):
    ok = subprocess.run([sys.executable, "scripts/verify_pdf_slides.py", good_deck])
    assert ok.returncode == 0
    bad = subprocess.run(
        [sys.executable, "scripts/verify_pdf_slides.py", bad_panel_deck]
    )
    assert bad.returncode == 1
