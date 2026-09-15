"""mark_resume_awaiting must be a true no-op when state is unchanged."""

import json

from app import discord_resume_registry as r


def _bootstrap(tmp_path, monkeypatch):
    p = tmp_path / "_discord_resume_awaiting.json"
    monkeypatch.setattr(r, "_PATH", p)
    return p


def test_repeat_mark_is_noop(tmp_path, monkeypatch):
    p = _bootstrap(tmp_path, monkeypatch)
    r.mark_resume_awaiting("123", "456", "hello")
    first = p.read_text(encoding="utf-8")
    r.mark_resume_awaiting("123", "456", "hello")
    assert p.read_text(encoding="utf-8") == first  # no rewrite at all


def test_ts_refresh_only_on_change(tmp_path, monkeypatch):
    p = _bootstrap(tmp_path, monkeypatch)
    r.mark_resume_awaiting("123", "456", "hello")
    ts1 = json.loads(p.read_text(encoding="utf-8"))["123"]["ts"]
    r.mark_resume_awaiting("123", "456", "hello")
    assert json.loads(p.read_text(encoding="utf-8"))["123"]["ts"] == ts1
    r.mark_resume_awaiting("123", "456", "hello world")
    assert json.loads(p.read_text(encoding="utf-8"))["123"]["text"] == "hello world"


def test_channel_change_persists(tmp_path, monkeypatch):
    p = _bootstrap(tmp_path, monkeypatch)
    r.mark_resume_awaiting("123", "456", "hello")
    r.mark_resume_awaiting("123", "999", "hello")
    assert json.loads(p.read_text(encoding="utf-8"))["123"]["channel_id"] == "999"
