"""Discord option-set registry (multiple-choice buttons) -- twin of Telegram."""

import json

from app import discord_resume_registry as r


def _bootstrap(tmp_path, monkeypatch):
    p = tmp_path / "_discord_resume_awaiting.json"
    monkeypatch.setattr(r, "_PATH", p)
    return p


def test_mark_and_lookup_options_roundtrip(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    r.mark_options("K7QM", "456", ["Run unit 3", "Stop"])
    assert r.lookup_options("K7QM") == ["Run unit 3", "Stop"]


def test_lookup_options_consumes_entry(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    r.mark_options("K7QM", "456", ["A"])
    assert r.lookup_options("K7QM") == ["A"]
    assert r.lookup_options("K7QM") is None  # single-fire


def test_lookup_options_unknown_is_none(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    assert r.lookup_options("NOPE") is None


def test_mark_options_ignores_blank(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    r.mark_options("", "456", ["A"])
    r.mark_options("K7QM", "", ["A"])
    r.mark_options("K7QM", "456", ["", "  "])
    assert r.lookup_options("K7QM") is None


def test_peek_options_does_not_consume(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    r.mark_options("K7QM", "456", ["A", "B"])
    assert r.peek_options("K7QM") == ["A", "B"]
    assert r.peek_options("K7QM") == ["A", "B"]  # still there
    assert r.lookup_options("K7QM") == ["A", "B"]


def test_options_coexist_with_resume_awaiting(tmp_path, monkeypatch):
    p = _bootstrap(tmp_path, monkeypatch)
    r.mark_resume_awaiting("900", "456", "choose one")
    r.mark_options("900", "456", ["A", "B"])
    data = json.loads(p.read_text(encoding="utf-8"))
    # both the resume fields and the options survive on one entry
    assert data["900"]["channel_id"] == "456"
    assert data["900"]["text"] == "choose one"
    assert data["900"]["options"] == ["A", "B"]


def test_persists_across_reload(tmp_path, monkeypatch):
    p = _bootstrap(tmp_path, monkeypatch)
    r.mark_options("K7QM", "456", ["A", "B"])
    # simulate a process restart: fresh load reads the same file
    assert json.loads(p.read_text(encoding="utf-8"))["K7QM"]["options"] == ["A", "B"]
