"""Tests for the public live-state projection.

These lean on one property above all others: the payload is built from an
*allowlist*, so a field the writer adds in future stays private until someone
edits ``PUBLIC_FIELDS`` deliberately.
"""

from __future__ import annotations

import pytest

from app import live_state


class _FakeProgress:
    def __init__(self, records):
        self.records = records
        self.scopes = list(records)

    def progress_scopes(self):
        return list(self.scopes)

    def progress_get(self, scope):
        return self.records.get(scope)

    def enabled(self):
        return True


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_STATE_PUBLIC", raising=False)
    assert live_state.enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_parsing_accepts_truthy(monkeypatch, raw):
    monkeypatch.setenv("LIVE_STATE_PUBLIC", raw)
    assert live_state.enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  "])
def test_flag_parsing_rejects_falsey(monkeypatch, raw):
    monkeypatch.setenv("LIVE_STATE_PUBLIC", raw)
    assert live_state.enabled() is False


def test_forbidden_fields_never_escape():
    """The whole security story in one test."""
    record = {
        "instruction": "sell 12 bags of cacao to Kirsten",
        "current_arg": "2024OSCAR_20260121_12",
        "current_tool": "lookup_qr_code",
        "round": 3,
        "elapsed": 12.5,
        "started_at": 1700000000.0,
        "queue_depth": 2,
        "done": ["scan_qr_from_file", {"tool": "read_context_file"}],
    }
    out = live_state._public_session(record)

    assert out is not None
    for forbidden in live_state.FORBIDDEN_FIELDS:
        assert forbidden not in out, f"{forbidden} leaked into the public payload"

    # The safe fields did survive.
    assert out["current_tool"] == "lookup_qr_code"
    assert out["round"] == 3
    assert out["done"] == ["scan_qr_from_file", "read_context_file"]


def test_unknown_field_is_private_by_default():
    """An allowlist fails closed: a new writer field must not appear."""
    record = {
        "current_tool": "x",
        "done": ["y"],
        "some_field_added_next_year": "must not leak",
        "api_token": "ghp_secret",
    }
    out = live_state._public_session(record)
    assert "some_field_added_next_year" not in out
    assert "api_token" not in out


def test_idle_session_is_suppressed():
    assert live_state._public_session({"round": 0, "elapsed": 0.0}) is None
    assert live_state._public_session({}) is None
    assert live_state._public_session(None) is None
    assert live_state._public_session("not a dict") is None


def test_done_is_bounded():
    record = {"current_tool": "t", "done": [f"step{i}" for i in range(200)]}
    out = live_state._public_session(record)
    assert out is not None
    assert len(out["done"]) == 20


def test_payload_degrades_without_redis(monkeypatch):
    """Redis unavailable must produce an empty section, not an exception."""

    def boom():
        raise RuntimeError("redis is down")

    monkeypatch.setattr(live_state.session_state, "progress_scopes", boom)
    monkeypatch.setattr(live_state, "_active_claims", lambda: ([], "claims file unavailable"))

    payload = live_state.live_state_payload()

    assert payload["sessions"] == []
    assert payload["session_count"] == 0
    assert payload["enabled"] is True
    assert "progress store unavailable" in payload["degraded"]
    assert payload["generated_at"].endswith("Z")


def test_payload_caps_session_count(monkeypatch):
    records = {
        f"scope{i}": {"current_tool": "t", "done": ["s"], "started_at": float(i)}
        for i in range(50)
    }
    fake = _FakeProgress(records)
    monkeypatch.setattr(live_state.session_state, "progress_scopes", fake.progress_scopes)
    monkeypatch.setattr(live_state.session_state, "progress_get", fake.progress_get)
    monkeypatch.setattr(live_state.session_state, "enabled", fake.enabled)
    monkeypatch.setattr(live_state, "_active_claims", lambda: ([], None))

    payload = live_state.live_state_payload()
    assert payload["session_count"] == live_state.MAX_SESSIONS
    # newest first
    assert payload["sessions"][0]["started_at"] == 49.0


def test_malformed_claims_file_is_tolerated(monkeypatch):
    monkeypatch.setattr(live_state, "_sessions", lambda: ([], None))
    monkeypatch.setattr(
        live_state, "_active_claims", lambda: ([], "claims file malformed")
    )
    payload = live_state.live_state_payload()
    assert payload["active_claims"] == []
    assert "claims file malformed" in payload["degraded"]
