"""Tests for Discord attachment handling (Tier-1 parity unit 2).

Mirrors tests/test_telegram_adapter.py + tests/test_gps_extraction.py: the
Discord adapter must be able to RECEIVE a file, download it, auto-extract its
content (PDF / image-OCR / Word), and feed that content to the brain.

Security invariants asserted here:
  * a non-governor's attachment is NAMED in the log but NEVER downloaded;
  * ``download_discord_file`` sends NO Authorization header (Discord CDN urls
    are pre-signed -- the bot token must never leave discord.com).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app import discord_adapter as da


def _att(
    filename="photo.jpg",
    url="https://cdn.discordapp.com/att/x/photo.jpg",
    content_type="image/jpeg",
    aid="a1",
):
    return {
        "id": aid,
        "filename": filename,
        "size": 123,
        "url": url,
        "content_type": content_type,
    }


# ── extraction helpers ────────────────────────────────────────────────────


def test_extract_attachments_present_and_absent():
    f = da.extract_attachments
    assert len(f({"attachments": [_att()]})) == 1
    assert f({}) == []
    assert f({"attachments": []}) == []
    assert f({"attachments": "nope"}) == []
    # entries without a url are unusable and dropped
    assert f({"attachments": [{"id": "a", "filename": "x"}]}) == []


def test_attachment_names_joins_filenames():
    assert (
        da.attachment_names(
            {"attachments": [_att(filename="a.pdf"), _att(filename="b.png")]}
        )
        == "a.pdf, b.png"
    )
    assert da.attachment_names({}) == ""


# ── download ──────────────────────────────────────────────────────────────


def test_download_discord_file_sends_no_auth_and_writes_file(monkeypatch, tmp_path):
    """The CDN fetch must NOT carry the bot token, and must land on disk."""
    seen = {}
    payload = b"hello-bytes"

    class _StreamResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield payload

    def fake_stream(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _StreamResp()

    monkeypatch.setattr(da, "_ATTACH_DIR", str(tmp_path))
    monkeypatch.setattr(da.httpx, "stream", fake_stream)

    dest = da.download_discord_file(_att(filename="note.pdf"))
    assert dest is not None and Path(dest).read_bytes() == payload
    assert seen["method"] == "GET"
    assert seen["url"] == "https://cdn.discordapp.com/att/x/photo.jpg"
    assert not seen["headers"]  # NO Authorization header -- pre-signed url
    assert dest.endswith(".pdf")


def test_download_discord_file_returns_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cdn down")

    monkeypatch.setattr(da.httpx, "stream", boom)
    assert da.download_discord_file(_att()) is None
    assert da.download_discord_file({"filename": "x"}) is None  # no url


# ── edit_message_text ─────────────────────────────────────────────────────


def test_edit_message_text_patches_when_live(monkeypatch):
    seen = {}
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    monkeypatch.setattr(
        da, "_api", lambda m, p, *a, **k: seen.update(m=m, p=p) or {"id": "1"}
    )
    assert da.edit_message_text("555", "msg1", "new text") is True
    assert seen["m"] == "PATCH"
    assert seen["p"] == "/channels/555/messages/msg1"


def test_edit_message_text_dry_run_and_no_id(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(da, "_api", lambda *a, **k: calls.update(n=calls["n"] + 1))
    monkeypatch.setattr(da.settings, "discord_dry_run", True)
    assert da.edit_message_text("555", "msg1", "x") is False
    monkeypatch.setattr(da.settings, "discord_dry_run", False)
    assert da.edit_message_text("555", "", "x") is False
    assert da.edit_message_text("555", "dry-run", "x") is False
    assert calls["n"] == 0


# ── _auto_process_attachment ──────────────────────────────────────────────


def _patch_status(monkeypatch, ta=da):
    monkeypatch.setattr(ta, "send_message", lambda *a, **k: ["status-1"])
    monkeypatch.setattr(ta, "edit_message_text", lambda *a, **k: True)


def test_auto_process_pdf(monkeypatch, tmp_path):
    _patch_status(monkeypatch)
    canned = json.dumps(
        {
            "status": "success",
            "page_count": 2,
            "total_chars": 11,
            "likely_scanned_pdf": False,
            "pages": [{"page": 1, "text": "hello"}, {"page": 2, "text": "world"}],
        }
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=0, stdout=canned, stderr=""
        ),
    )

    summary = da._auto_process_attachment(str(tmp_path / "doc.pdf"), "555", "sess-1")
    assert summary is not None
    assert "Type: PDF (2 pages, 11 chars)" in summary
    assert "hello" in summary and "world" in summary


def test_auto_process_image_ocr_and_gps(monkeypatch, tmp_path):
    _patch_status(monkeypatch)
    canned = json.dumps(
        {
            "status": "success",
            "text": "scanned words",
            "avg_confidence": 92,
            "quality": "good",
        }
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=a, returncode=0, stdout=canned, stderr=""
        ),
    )

    import app.tools.qr_scanner as qr

    monkeypatch.setattr(
        qr,
        "extract_gps_from_image",
        lambda p: {
            "lat": -3.09,
            "lon": -52.09,
            "alt": None,
            "timestamp": "2026:09:02 22:21:12",
        },
    )

    summary = da._auto_process_attachment(str(tmp_path / "pic.jpg"), "555", "s")
    assert "Type: Image (OCR confidence: 92%, quality: good)" in summary
    assert "scanned words" in summary
    assert "GPS: -3.09, -52.09" in summary
    assert "Captured: 2026:09:02 22:21:12" in summary


def test_auto_process_unknown_type_returns_none(monkeypatch, tmp_path):
    _patch_status(monkeypatch)
    assert da._auto_process_attachment(str(tmp_path / "f.xyz"), "555", "s") is None


# ── dispatch wiring ───────────────────────────────────────────────────────


def _msg(user_id="999", content="hi", bot=False, channel="555", attachments=None):
    m = {
        "id": "msg1",
        "channel_id": channel,
        "content": content,
        "author": {"id": user_id, "username": "u", "bot": bot},
    }
    if attachments is not None:
        m["attachments"] = attachments
    return m


def test_handle_message_bare_attachment_is_dispatched(monkeypatch):
    """A file with NO text must still reach the brain (was silently dropped)."""
    seen = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da, "_ingest_attachments", lambda atts, ch, sid, txt: "PROMPT:" + txt
    )
    monkeypatch.setattr(da, "call_chat", lambda p, s, k: seen.update(prompt=p) or "ok")
    monkeypatch.setattr(da, "send_message", lambda ch, t: [])
    da.handle_message(_msg(content="", attachments=[_att()]), {"999"}, "KEY", "1", "42")
    assert seen["prompt"] == "PROMPT:"


def test_handle_message_governor_attachment_ingested(monkeypatch):
    seen = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(
        da,
        "_ingest_attachments",
        lambda atts, ch, sid, txt: f"INGEST({len(atts)}):{txt}",
    )
    monkeypatch.setattr(da, "call_chat", lambda p, s, k: seen.update(prompt=p) or "ok")
    monkeypatch.setattr(da, "send_message", lambda ch, t: [])
    da.handle_message(
        _msg(content="look", attachments=[_att(), _att(aid="a2")]),
        {"999"},
        "KEY",
        "1",
        "42",
    )
    assert seen["prompt"] == "INGEST(2):look"


def test_handle_message_non_governor_attachment_never_downloaded(monkeypatch):
    """Data/instruction boundary: a member's file is named in the log, NOT fetched."""
    observed = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    monkeypatch.setattr(
        da, "log_observed_message", lambda txt, sid, key, who: observed.update(txt=txt)
    )
    monkeypatch.setattr(
        da,
        "download_discord_file",
        lambda a: (_ for _ in ()).throw(AssertionError("downloaded!")),
    )
    monkeypatch.setattr(
        da,
        "_ingest_attachments",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ingested!")),
    )
    da.handle_message(
        _msg(user_id="578", content="here", attachments=[_att()]),
        set(),
        "KEY",
        "1",
        "42",
    )
    assert "attachments: photo.jpg" in observed["txt"]
