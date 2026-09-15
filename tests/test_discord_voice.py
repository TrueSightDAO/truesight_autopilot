"""Unit tests for the Discord voice I/O helpers (Tier-2 parity #4).

No network: transcription + synthesis are monkeypatched. Only the pure
attachment/URL helpers and the reply orchestration are exercised.
"""

from __future__ import annotations

from app import discord_adapter as da

# -- pure helpers ------------------------------------------------------------


def test_is_voice_attachment_by_content_type():
    assert da.is_voice_attachment(
        {"content_type": "audio/ogg", "filename": "voice-message.ogg"}
    )
    assert da.is_voice_attachment(
        {"content_type": "audio/mpeg", "filename": "clip.mp3"}
    )


def test_is_voice_attachment_by_extension():
    assert da.is_voice_attachment({"filename": "note.mp3"})
    assert da.is_voice_attachment({"filename": "sound.OGG"})  # case-insensitive


def test_is_voice_attachment_rejects_images_and_docs():
    assert not da.is_voice_attachment(
        {"content_type": "image/png", "filename": "a.png"}
    )
    assert not da.is_voice_attachment(
        {"content_type": "application/pdf", "filename": "a.pdf"}
    )
    assert not da.is_voice_attachment({})


def test_extract_voice_attachment_picks_audio():
    atts = [
        {"content_type": "image/png", "filename": "x.png"},
        {"content_type": "audio/ogg", "filename": "v.ogg"},
    ]
    picked = da.extract_voice_attachment(atts)
    assert picked is not None and picked["filename"] == "v.ogg"
    assert da.extract_voice_attachment([{"filename": "x.png"}]) is None


def test_extract_urls():
    assert da.extract_urls("see https://a.com/x and http://b.io") == [
        "https://a.com/x",
        "http://b.io",
    ]
    assert da.extract_urls("") == []


# -- reply orchestration -----------------------------------------------------


def _patch_voice(monkeypatch, calls, mp3="/tmp/x.mp3"):
    monkeypatch.setattr(da, "detect_language", lambda *a, **k: "en")
    monkeypatch.setattr(da, "synthesize_voice", lambda *a, **k: mp3)
    monkeypatch.setattr(
        da, "send_voice", lambda ch, p, cap="": calls["voice"].append((ch, p, cap))
    )
    monkeypatch.setattr(
        da, "send_message", lambda ch, t: calls["msg"].append((ch, t)) or []
    )


def test_handle_voice_reply_text_already_sent(monkeypatch):
    calls = {"voice": [], "msg": []}
    _patch_voice(monkeypatch, calls)
    da._handle_voice_reply(
        "c1",
        "Here is the answer https://x.com",
        transcribed_text="ok",
        text_already_sent=True,
    )
    assert calls["voice"] and calls["voice"][0][1] == "/tmp/x.mp3"
    assert calls["msg"] == []  # text not re-sent
    assert "https://x.com" in calls["voice"][0][2]  # url follow-up in caption


def test_handle_voice_reply_fallback_sends_text(monkeypatch):
    calls = {"voice": [], "msg": []}
    _patch_voice(monkeypatch, calls)
    da._handle_voice_reply("c1", "answer with no link", text_already_sent=False)
    assert calls["msg"] == [("c1", "answer with no link")]
    assert calls["voice"] and calls["voice"][0][2] == ""  # no urls -> empty caption


def test_handle_voice_reply_synthesis_failure_sends_text(monkeypatch):
    calls = {"voice": [], "msg": []}
    _patch_voice(monkeypatch, calls, mp3=None)
    da._handle_voice_reply("c1", "answer", text_already_sent=False)
    assert calls["msg"] == [("c1", "answer")]
    assert calls["voice"] == []
