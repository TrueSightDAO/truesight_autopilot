"""Tests for Discord live-progress visibility (Tier-1 parity #1).

Mirrors tests/test_telegram_adapter.py's progress coverage: the Discord adapter
must show interim progress (a single status message, EDITED in place) during a
long turn instead of going silent between dispatch and the final reply.
"""

from __future__ import annotations

import json

import httpx

from app import discord_adapter as da


class _Settings:
    def __init__(self, dry=False):
        self.discord_dry_run = dry
        self.autopilot_chat_url = "http://brain.local:8000"


# ── _label / edit plumbing ───────────────────────────────────────────────────


def test_constants_present():
    assert da._MESSAGE_LIMIT == 2000
    assert da._DEPLOY_MARKER == "/tmp/.autopilot_deployed"


def test_edit_message_text_dry_run_returns_false(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=True))
    assert da.edit_message_text("c1", "m1", "hi") is False


def test_edit_message_text_no_id_returns_false(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    assert da.edit_message_text("c1", "", "hi") is False
    assert da.edit_message_text("c1", "dry-run", "hi") is False


def test_edit_message_text_patches(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    calls = {}

    def fake_api(method, path, payload=None):
        calls["method"], calls["path"], calls["payload"] = method, path, payload
        return {"id": "m1"}

    monkeypatch.setattr(da, "_api", fake_api)
    assert da.edit_message_text("c1", "m1", "hello") is True
    assert calls["method"] == "PATCH"
    assert calls["path"] == "/channels/c1/messages/m1"
    assert calls["payload"]["content"] == "hello"


def test_delete_message_dry_run_and_patch(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=True))
    assert da.delete_message("c1", "m1") is False
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    seen = {}
    monkeypatch.setattr(
        da,
        "_api",
        lambda m, p, payload=None: seen.update(m=m, p=p) or {"id": "m1"},
    )
    assert da.delete_message("c1", "m1") is True
    assert (seen["m"], seen["p"]) == ("DELETE", "/channels/c1/messages/m1")


# ── brain probe helpers ──────────────────────────────────────────────────────


def test_wait_for_brain_true_when_healthy(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings())
    monkeypatch.setattr(
        da.httpx,
        "get",
        lambda url, timeout=None: httpx.Response(
            200, request=httpx.Request("GET", url)
        ),
    )
    assert da._wait_for_brain() is True
    assert da._LAST_BRAIN_PROBE_ERROR == ""


def test_wait_for_brain_false_then_message_down(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings())
    monkeypatch.setattr(
        da,
        "time",
        type(
            "T",
            (),
            {"sleep": staticmethod(lambda s: None), "time": staticmethod(lambda: 0.0)},
        ),
    )

    def boom(url, timeout=None):
        raise httpx.ConnectError(
            "Connection refused", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(da.httpx, "get", boom)
    assert da._wait_for_brain(max_attempts=2) is False
    msg = da._brain_unavailable_message()
    assert "DOWN" in msg


def test_brain_unavailable_message_names_redeploy(monkeypatch, tmp_path):
    marker = tmp_path / ".autopilot_deployed"
    marker.write_text("x")
    monkeypatch.setattr(da, "_DEPLOY_MARKER", str(marker))
    assert "redeploy" in da._brain_unavailable_message()


# ── call_chat_with_progress end-to-end ───────────────────────────────────────


def _sse(*events):
    """Build a fake httpx streaming response from SSE event dicts.

    httpx's ``iter_lines()`` yields ``str`` -- the fake must too, or the
    adapter's ``line.startswith("data: ")`` raises on a bytes/str mismatch.
    """
    body = "\n".join(f"data: {json.dumps(e)}" for e in events) + "\n"

    class _Resp:
        status_code = 200

        def iter_lines(self):
            yield from body.split("\n")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    return _Stream()


def test_progress_edits_status_in_place(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "_wait_for_brain", lambda *a, **k: True)
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")

    sent = []
    edited = []

    def fake_send(channel_id, text):
        sent.append(text)
        return ["status-1"]

    def fake_edit(channel_id, mid, text):
        edited.append((mid, text))
        return True

    monkeypatch.setattr(da, "send_message", fake_send)
    monkeypatch.setattr(da, "edit_message_text", fake_edit)
    monkeypatch.setattr(
        da.httpx,
        "stream",
        lambda *a, **k: _sse(
            {"type": "heartbeat", "round": 1},
            {"type": "tool", "tool": "ssh_run", "status": "calling"},
            {"type": "done", "response": "all done"},
        ),
    )

    resp, shown = da.call_chat_with_progress("c1", "hello", "s1", "pk")
    assert resp == "all done"
    assert shown is True
    # exactly ONE status message posted; everything else is an EDIT of it
    assert sent == ["\U0001f504 Thinking\u2026"]
    assert all(mid == "status-1" for mid, _ in edited)
    final_edits = [t for _, t in edited]
    assert "all done" in final_edits
    assert any("ssh run" in t for t in final_edits)


def test_progress_falls_back_when_status_send_fails(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(da, "send_message", lambda c, t: [])
    monkeypatch.setattr(da, "call_chat", lambda *a, **k: "blocking reply")
    resp, shown = da.call_chat_with_progress("c1", "hello", "s1", "pk")
    assert resp == "blocking reply"
    assert shown is False  # caller must post it


def test_progress_hiccups_on_brain_down(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(da, "send_message", lambda c, t: ["status-1"])
    monkeypatch.setattr(da, "_wait_for_brain", lambda *a, **k: False)
    edited = []
    monkeypatch.setattr(
        da, "edit_message_text", lambda c, m, t: edited.append(t) or True
    )
    resp, shown = da.call_chat_with_progress("c1", "hello", "s1", "pk")
    assert shown is True
    assert (
        edited
        and "restart" in edited[0].lower()
        or "DOWN" in edited[0]
        or "BUSY" in edited[0]
    )


def test_handle_message_uses_progress_and_removes_reaction(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    events = {}
    monkeypatch.setattr(
        da, "add_reaction", lambda c, m: events.setdefault("add", (c, m)) or True
    )
    monkeypatch.setattr(
        da, "remove_reaction", lambda c, m: events.setdefault("rm", (c, m)) or True
    )

    class _TI:
        def __init__(self, c):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(da, "TypingIndicator", _TI)
    monkeypatch.setattr(
        da, "call_chat_with_progress", lambda c, p, s, pk: ("answer", True)
    )
    posted = []
    monkeypatch.setattr(da, "send_message", lambda c, t: posted.append(t) or ["x"])

    da.handle_message(
        {
            "author": {"id": "42", "username": "gary"},
            "channel_id": "c1",
            "id": "m1",
            "content": "hi",
            "mentions": [],
        },
        {"42"},
        "pk",
        "g1",
        "bot9",
    )
    assert events["add"] == ("c1", "m1")
    assert events["rm"] == ("c1", "m1")  # always removed, even on success
    assert posted == []  # text already shown via edit -> not re-posted


# ── stale removed-approval-gate suffix (regression) ──────────────────────────
# The DAO approval gate was REMOVED 2026-06-18: a signed submission IS the
# authorization. The adapter must NOT append "open the DApp chat to
# approve/reject" to a response just because the brain still emits a
# ``proposal`` object -- that strand a governor on a button that does nothing.


def test_proposal_does_not_append_stale_approval_prompt(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "_wait_for_brain", lambda *a, **k: True)
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")
    monkeypatch.setattr(da, "send_message", lambda c, t: ["status-1"])
    edited = []
    monkeypatch.setattr(
        da, "edit_message_text", lambda c, m, t: edited.append(t) or True
    )
    monkeypatch.setattr(
        da.httpx,
        "stream",
        lambda *a, **k: _sse(
            {"type": "done", "response": "submitted", "proposal": {"x": 1}},
        ),
    )
    resp, shown = da.call_chat_with_progress("c1", "hi", "s1", "pk")
    assert resp == "submitted"
    assert "approve" not in resp.lower()
    assert "DApp" not in resp
    assert "needs approval" not in resp
