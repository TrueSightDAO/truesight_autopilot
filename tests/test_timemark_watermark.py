"""Tests for the Timemark watermark reader (OCR-pattern detection).

Covers parse_timemark_text against the real field-photo ground truth captured
2026-10-15 (codes KN2YBE34RMWY6C / P1P4TWTDPB3YGE / BLU3316YHGW2DL) and the
read_timemark_watermark orchestration with injected ocr_fn/vision_fn (no
network, no tesseract).
"""

from __future__ import annotations

from PIL import Image

from app.tools.timemark_watermark import (
    format_summary,
    parse_timemark_text,
    read_timemark_watermark,
)

# Real bottom-left + right-edge text as read off the actual photos.
BOTTOM = "Seg, 12 out. 2026 07:14\nPará\n3.522716°S, 51.574659°W\n"
RIGHT = "KN2YBE34RMWY6C\nTimemark Verified\n"


def test_parse_extracts_code_gps_date_from_ground_truth():
    f = parse_timemark_text(BOTTOM + RIGHT)
    assert f["code"] == "KN2YBE34RMWY6C"
    assert f["lat"] == "3.522716" and f["lat_ref"] == "S"
    assert f["lon"] == "51.574659" and f["lon_ref"] == "W"
    assert f["capture_time"] == "Seg, 12 out. 2026 07:14"


def test_parse_handles_no_degree_symbol_and_variant_codes():
    f = parse_timemark_text("3.522797°S, 51.574546°W\nP1P4TWTDPB3YGE\n")
    assert f["code"] == "P1P4TWTDPB3YGE"
    g = parse_timemark_text("3.522940°S, 51.574596°W BLU3316YHGW2DL")
    assert g["code"] == "BLU3316YHGW2DL"


def test_code_regex_ignores_gps_numbers_and_short_tokens():
    # pure-digit GPS fragments must NOT be mistaken for a code
    f = parse_timemark_text("3.5229195, -51.5749705 2026 07:1412")
    assert "code" not in f
    # 'Timemark' / 'Verified' are letters-only -> not a code
    assert "code" not in parse_timemark_text("Timemark Verified Para")


def test_read_orchestration_prefers_local_ocr_then_vision(tmp_path):
    img = tmp_path / "field.jpg"
    Image.new("RGB", (400, 600), "green").save(img)

    called = {"vision": 0}

    def ocr_ok(_crop):
        return BOTTOM + RIGHT

    def vision_should_not_run(_regions):
        called["vision"] += 1
        return ""

    out = read_timemark_watermark(
        str(img), vision_fn=vision_should_not_run, ocr_fn=ocr_ok
    )
    assert out["code"] == "KN2YBE34RMWY6C"
    assert out["method"] == "tesseract"
    assert called["vision"] == 0
    assert out["summary"].startswith("[Timemark]")


def test_read_falls_back_to_vision_when_ocr_blind(tmp_path):
    img = tmp_path / "field.jpg"
    Image.new("RGB", (400, 600), "green").save(img)

    def ocr_blind(_crop):
        return ""  # light-on-foliage: tesseract reads nothing

    def vision_reads(_regions):
        return "Seg, 12 out. 2026 07:27\nPará\n3.522797°S, 51.574546°W\nP1P4TWTDPB3YGE"

    out = read_timemark_watermark(str(img), vision_fn=vision_reads, ocr_fn=ocr_blind)
    assert out["code"] == "P1P4TWTDPB3YGE"
    assert out["method"] == "vision"


def test_read_returns_empty_when_no_watermark(tmp_path):
    img = tmp_path / "plain.jpg"
    Image.new("RGB", (400, 600), "blue").save(img)
    assert (
        read_timemark_watermark(str(img), vision_fn=lambda r: "", ocr_fn=lambda c: "")
        == {}
    )


def test_format_summary_shape():
    s = format_summary(
        {
            "capture_time": "Seg, 12 out. 2026 07:14",
            "lat": "3.522716",
            "lat_ref": "S",
            "lon": "51.574659",
            "lon_ref": "W",
            "code": "KN2YBE34RMWY6C",
        }
    )
    assert s.startswith("[Timemark]")
    assert "KN2YBE34RMWY6C" in s and "3.522716" in s


def test_crop_regions_returns_two_images(tmp_path):
    from app.tools.timemark_watermark import crop_watermark_regions

    img = tmp_path / "f.jpg"
    Image.new("RGB", (800, 1200), "green").save(img)
    r = crop_watermark_regions(str(img))
    assert set(r) == {"bottom", "right"}
    assert r["bottom"].width == 800
