"""Tests for the Media Archives Pipeline dashboard data endpoint."""

import json
from pathlib import Path

from app.media_archive_pipeline import _status_of, _parse_sidecar


def test_status_of_uploaded():
    assert _status_of({"yt_id": "abc123"}) == "uploaded"


def test_status_of_error():
    assert _status_of({"error": "quota exceeded"}) == "error"


def test_status_of_needs_metadata():
    assert _status_of({"sha256": "x", "gps": "y"}) == "needs_metadata"  # no title


def test_status_of_pending():
    assert _status_of({"sha256": "x", "gps": "y", "title": "z"}) == "pending"


def test_parse_sidecar_bad_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert _parse_sidecar(p) == {}


def test_parse_sidecar_not_dict(tmp_path: Path):
    p = tmp_path / "list.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert _parse_sidecar(p) == {}
