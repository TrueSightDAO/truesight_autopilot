"""Tests for app/resume_registry.py — resume-awaiting message registry."""

import json
import time

import pytest

from app import resume_registry as rr
from app.config import settings

_PATH = settings.session_log_dir / "_resume_awaiting.json"


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway file + fresh state per test."""
    monkeypatch.setattr(rr, "_PATH", tmp_path / "_resume_awaiting.json")
    monkeypatch.setattr(rr, "_lock", __import__("threading").Lock())
    yield
    # ensure no cross-test leakage


def test_looks_resume_awaiting_text():
    assert rr.looks_resume_awaiting("RESUME HERE") is True
    assert rr.looks_resume_awaiting("resume here") is True  # case-insensitive
    assert rr.looks_resume_awaiting("📌 RESUME HERE = PR1") is True
    assert rr.looks_resume_awaiting("plain turn report") is False
    assert rr.looks_resume_awaiting("") is False
    assert rr.looks_resume_awaiting(None) is False


def test_looks_resume_awaiting_pin_marker_alone():
    # The 📌 pin marker alone also declares a resume point (handoff convention).
    assert rr.looks_resume_awaiting("📌 RESUME HERE") is True
    assert rr.looks_resume_awaiting("📌") is True
    assert rr.looks_resume_awaiting("Here is a 📌 reminder") is True
    assert rr.looks_resume_awaiting("Pinned: RESUME HERE next turn") is True


def test_mark_then_lookup_roundtrip():
    rr.mark_resume_awaiting(1001, 15728, "ready — reply go for it")
    assert rr.is_resume_awaiting(1001) is True
    entry = rr.lookup(1001)
    assert entry == {"thread_id": "15728", "text": "ready — reply go for it"}


def test_lookup_consumes_entry():
    rr.mark_resume_awaiting(1001, 15728, "go proposal")
    assert rr.lookup(1001) is not None
    assert rr.lookup(1001) is None  # consumed
    assert rr.is_resume_awaiting(1001) is False


def test_unknown_message_is_not_resume_awaiting():
    assert rr.is_resume_awaiting(424242) is False
    assert rr.lookup(424242) is None


def test_requires_both_ids():
    rr.mark_resume_awaiting("", 15728, "x")  # no message id
    rr.mark_resume_awaiting(1001, "", "x")  # no thread id
    assert rr.is_resume_awaiting("") is False
    assert rr.is_resume_awaiting(1001) is False


def test_persists_across_reload(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    monkeypatch.setattr(rr, "_PATH", path)
    monkeypatch.setattr(rr, "_lock", __import__("threading").Lock())
    rr.mark_resume_awaiting(77, 5, "text")
    # simulate a fresh process: new module state reading the same file
    data = json.loads(path.read_text(encoding="utf-8"))
    assert str(77) in data
    assert data["77"]["thread_id"] == "5"


def test_ttl_expiry_prunes(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    monkeypatch.setattr(rr, "_PATH", path)
    monkeypatch.setattr(rr, "_lock", __import__("threading").Lock())
    rr.mark_resume_awaiting(1, 10)
    # age the entry beyond TTL, then any load/lookup prunes it
    data = json.loads(path.read_text(encoding="utf-8"))
    data["1"]["ts"] = time.time() - rr._DEFAULT_TTL_SECONDS - 60
    path.write_text(json.dumps(data), encoding="utf-8")
    assert rr.lookup(1) is None  # TTL-expired -> pruned on lookup


def test_ttl_recent_survives(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    monkeypatch.setattr(rr, "_PATH", path)
    monkeypatch.setattr(rr, "_lock", __import__("threading").Lock())
    rr.mark_resume_awaiting(2, 20, "fresh")
    assert rr.lookup(2) is not None


def test_corrupt_file_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "reg.json"
    monkeypatch.setattr(rr, "_PATH", path)
    monkeypatch.setattr(rr, "_lock", __import__("threading").Lock())
    path.write_text("{not json", encoding="utf-8")
    assert rr.lookup(3) is None
    assert rr.is_resume_awaiting(3) is False
    # and a new mark still works
    rr.mark_resume_awaiting(4, 40, "ok")
    assert rr.lookup(4) is not None
