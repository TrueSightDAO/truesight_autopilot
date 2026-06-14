"""Thread-status enrichment: thread_id + clickable Telegram deep-link + topic name."""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.topic_names as tn
    from app.vault_routes import _enrich_track
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def test_enrich_track_derives_thread_id_and_link():
    t = _enrich_track(
        {
            "id": "tg:-1003919341801:780",
            "metadata": {"session_id": "tg:-1003919341801:780"},
        }
    )
    assert t["thread_id"] == "780"
    assert t["chat_id"] == "-1003919341801"
    assert t["telegram_link"] == "https://t.me/c/3919341801/780"


def test_enrich_track_non_telegram_is_untouched():
    t = _enrich_track({"id": "background-loop-x", "metadata": {}})
    assert "thread_id" not in t and "telegram_link" not in t


def test_topic_name_record_and_resolve(monkeypatch, tmp_path):
    monkeypatch.setattr(tn, "_PATH", tmp_path / "_topic_names.json")
    assert tn.get_topic_name("780") is None
    tn.record_topic_name("780", "Stream of Consciousness")
    assert tn.get_topic_name("780") == "Stream of Consciousness"
    tn.record_topic_name("780", "Stream of Consciousness")  # idempotent
    assert tn.get_topic_name("780") == "Stream of Consciousness"


def test_enrich_track_includes_recorded_name(monkeypatch, tmp_path):
    monkeypatch.setattr(tn, "_PATH", tmp_path / "_topic_names.json")
    tn.record_topic_name("2744", "Governance & Vault")
    t = _enrich_track(
        {
            "id": "tg:-1003919341801:2744",
            "metadata": {"session_id": "tg:-1003919341801:2744"},
        }
    )
    assert t["thread_name"] == "Governance & Vault"
    assert t["telegram_link"].endswith("/2744")
