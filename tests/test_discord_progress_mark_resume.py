"""Progress edits must NOT mark the status message resume-awaiting.

Regression guard for the `awaiting_stale` loop: `edit_message_text` used to
flag EVERY edited message id for the reaction go-signal. `_edit()` calls it for
each streaming status update, so during a long turn the registry entry's `ts`
was rewritten on every tool call. The status message therefore always looked
like "a prompt posted seconds ago", while the session log is only written at
turn end -- so `loop_status` reported `awaiting_stale` at a governor who had
already replied, and the nudge repeated forever.

It also corrupted the reaction go-signal itself: reacting to a progress tick
would resume with stored text like `'ssh run ...'` instead of an instruction.
"""

from __future__ import annotations

import json

from app import discord_adapter as da


class _Settings:
    def __init__(self, dry=False):
        self.discord_dry_run = dry
        self.autopilot_chat_url = "http://brain.local:8000"


def _sse(*events):
    """Fake httpx streaming response built from SSE event dicts."""
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


def test_interim_progress_edits_do_not_mark_resume_awaiting(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    monkeypatch.setattr(da, "_wait_for_brain", lambda *a, **k: True)
    monkeypatch.setattr(da, "create_jwt", lambda *a, **k: "jwt")

    seen = []

    def fake_edit(channel_id, mid, text, **kwargs):
        seen.append((text, kwargs.get("mark_resume", True)))
        return True

    monkeypatch.setattr(da, "send_message", lambda c, t: ["status-1"])
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

    da.call_chat_with_progress("c1", "hello", "s1", "pk")

    interim = [(t, m) for t, m in seen if "ssh run" in t]
    assert interim, "expected an interim progress edit"
    assert all(m is False for _, m in interim), interim

    # The FINAL reply must still mark, so an emoji reaction can resume.
    assert [m for t, m in seen if t == "all done"] == [True]


def test_edit_message_text_honours_mark_resume(monkeypatch):
    monkeypatch.setattr(da, "settings", _Settings(dry=False))
    calls = []
    monkeypatch.setattr(
        da.discord_resume_registry,
        "mark_resume_awaiting",
        lambda *a, **k: calls.append(a),
    )
    monkeypatch.setattr(da, "_api", lambda *a, **k: {"id": "m1"})

    assert da.edit_message_text("c1", "m1", "progress", mark_resume=False) is True
    assert calls == []

    assert da.edit_message_text("c1", "m1", "final answer") is True
    assert len(calls) == 1
