"""Unit tests for the Google Sheets tool (Sheets API mocked)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools import google_sheets as gs


def _fake_creds():
    """Just a sentinel object — we don't call any methods on it."""
    return object()


def test_credentials_missing_returns_error(monkeypatch):
    monkeypatch.setattr(gs, "load_credentials", lambda *a, **k: None)
    out = json.loads(gs.read_google_sheet("sheetid", "A1:B2"))
    assert out["status"] == "error"
    assert out["reason"] == "credentials missing"


def test_missing_args_returns_error():
    out = json.loads(gs.read_google_sheet("", "A1:B2"))
    assert out["status"] == "error"
    out = json.loads(gs.read_google_sheet("sheetid", ""))
    assert out["status"] == "error"


def test_happy_path_returns_rows(monkeypatch):
    monkeypatch.setattr(gs, "load_credentials", lambda *a, **k: _fake_creds())

    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "range": "Sheet1!A1:B2",
        "values": [["Header1", "Header2"], ["v1", "v2"]],
    }

    with patch("googleapiclient.discovery.build", return_value=fake_service) as build_mock:
        out = json.loads(gs.read_google_sheet("sheetid", "Sheet1!A1:B2"))

    assert out["status"] == "ok"
    assert out["row_count"] == 2
    assert out["values"][0] == ["Header1", "Header2"]
    assert out["truncated"] is False
    build_mock.assert_called_once()


def test_truncation_caps_cells(monkeypatch):
    monkeypatch.setattr(gs, "load_credentials", lambda *a, **k: _fake_creds())
    # 600 rows × 10 cols = 6000 cells; cap is 5000.
    big_values = [[f"r{i}c{j}" for j in range(10)] for i in range(600)]

    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
        "range": "Sheet1!A1:J600",
        "values": big_values,
    }

    with patch("googleapiclient.discovery.build", return_value=fake_service):
        out = json.loads(gs.read_google_sheet("sheetid", "Sheet1!A1:J600"))

    assert out["status"] == "ok"
    assert out["truncated"] is True
    total = sum(len(r) for r in out["values"])
    assert total <= 5000


def test_api_failure_returns_error(monkeypatch):
    monkeypatch.setattr(gs, "load_credentials", lambda *a, **k: _fake_creds())

    fake_service = MagicMock()
    fake_service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = RuntimeError(
        "permission denied"
    )

    with patch("googleapiclient.discovery.build", return_value=fake_service):
        out = json.loads(gs.read_google_sheet("sheetid", "A1"))

    assert out["status"] == "error"
    assert "permission denied" in out["reason"]
