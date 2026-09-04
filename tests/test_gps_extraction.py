"""Tests for GPS extraction from images and the HEIC/GPS attachment wiring.

Covers extract_gps_from_image (JPEG EXIF GPS IFD -> decimal degrees) and the
Telegram adapter's HEIC -> JPEG conversion + GPS surfacing path inside
_auto_process_attachment.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from app.tools.qr_scanner import extract_gps_from_image


def _make_gps_jpeg(path: Path) -> Path:
    """Write a tiny JPEG whose EXIF GPS IFD mirrors a phone photo."""
    im = Image.new("RGB", (16, 16), "red")
    exif = Image.Exif()
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[1] = "S"
    gps[2] = [3.0, 5.0, 39.12]  # 3 deg 5' 39.12" S
    gps[3] = "W"
    gps[4] = [52.0, 5.0, 42.65]  # 52 deg 5' 42.65" W
    gps[7] = [22, 21, 12]  # GPSTimeStamp 22:21:12
    gps[29] = "2026:09:02"  # GPSDateStamp
    im.save(path, format="JPEG", quality=90, exif=exif)
    return path


def test_extract_gps_from_image_reads_decimal_and_timestamp(tmp_path):
    jpg = _make_gps_jpeg(tmp_path / "gps.jpg")
    gps = extract_gps_from_image(str(jpg))
    assert gps is not None
    assert gps["lat"] == pytest.approx(-3.0942, abs=1e-3)
    assert gps["lon"] == pytest.approx(-52.0952, abs=1e-3)
    assert gps["lat_ref"] == "S"
    assert gps["lon_ref"] == "W"
    assert gps["timestamp"] == "2026:09:02 22:21:12"


def test_extract_gps_from_image_none_without_gps(tmp_path):
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (16, 16), "blue").save(plain, format="JPEG")
    assert extract_gps_from_image(str(plain)) is None


def test_extract_gps_from_image_none_for_missing_file(tmp_path):
    assert extract_gps_from_image(str(tmp_path / "nope.jpg")) is None


def test_auto_process_attachment_heic_converts_and_surfaces_gps(monkeypatch, tmp_path):
    from app import telegram_adapter as ta
    import app.tools.qr_scanner as qr_scanner

    gps_jpg = _make_gps_jpeg(tmp_path / "converted.jpg")
    converted: dict[str, str] = {}

    def fake_convert(heic_path: str, jpg_path: str | None = None) -> str:
        converted["src"] = heic_path
        return str(gps_jpg)

    monkeypatch.setattr(qr_scanner, "convert_heic_to_jpg", fake_convert)
    monkeypatch.setattr(ta, "send_message", lambda *a, **k: 1)
    monkeypatch.setattr(ta, "edit_message_text", lambda *a, **k: None)

    # Intercept the OCR subprocess the adapter shells out to: return a canned
    # success payload instead of running tesseract on a 16x16 fixture.
    canned = json.dumps(
        {
            "status": "success",
            "text": "hello ocr",
            "avg_confidence": 95,
            "quality": "good",
        }
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=canned, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    summary = ta._auto_process_attachment(
        str(tmp_path / "photo.heic"), 123, None, "sess-1"
    )

    assert summary is not None
    assert converted["src"].endswith("photo.heic")
    assert "HEIC converted to JPEG (EXIF/GPS preserved)" in summary
    assert "GPS: -3.0942, -52.095181" in summary
    assert "Captured: 2026:09:02 22:21:12" in summary
    assert "hello ocr" in summary
