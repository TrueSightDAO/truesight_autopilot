"""Defensive self-heal for the 2026-06-12 'empty response' brick.

(a) A pile of consecutive user messages (turns that never produced an assistant
    reply during an outage) made DeepSeek return empty, which self-perpetuated.
    _sanitise_tool_messages now collapses them.
(b) The generic 'this MAY be a handoff' prefix was injected on EVERY message to
    any unregistered thread (e.g. Stream-of-consciousness) — confusing noise.
    Now only injected on a go-signal / plan reference.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


# ── (a) collapse consecutive user messages ────────────────────────────────────


def test_collapses_consecutive_user_messages():
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    m._sanitise_tool_messages(history)
    roles = [x["role"] for x in history]
    assert roles == ["user", "assistant", "user"]  # the 3-run collapsed to 1
    merged = history[-1]["content"]
    assert "a" in merged and "b" in merged and "c" in merged  # content preserved


def test_collapse_dedups_identical_pokes():
    history = [{"role": "user", "content": "are you there?"}] * 5
    m._sanitise_tool_messages(history)
    assert len(history) == 1
    assert history[0]["content"] == "are you there?"  # 5 identical → one


def test_wellformed_alternation_untouched():
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    import json

    before = json.dumps(history)
    m._sanitise_tool_messages(history)
    assert json.dumps(history) == before


# ── (b) generic handoff prefix only on go-signals ─────────────────────────────


def _adapter():
    try:
        import app.telegram_adapter as ta

        return ta
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"telegram_adapter import unavailable: {exc}")


def test_go_signal_detection():
    ta = _adapter()
    for s in ["go for it", "go", "proceed", "resume the plan", "ship it", "continue"]:
        assert ta._looks_like_go_signal(s), s
    for s in [
        "just thinking out loud about cacao",
        "how was your day",
        "the weather is nice",
    ]:
        assert not ta._looks_like_go_signal(s), s


def test_unregistered_thread_no_prefix_on_normal_chat(monkeypatch):
    ta = _adapter()
    # Patches _handoff_plan_and_auto_start_for_thread — the function
    # _handoff_prefix actually calls since the 2026-07-21 Auto-start refactor
    # (_handoff_plan_for_thread is now just a thin wrapper over it).
    monkeypatch.setattr(
        ta, "_handoff_plan_and_auto_start_for_thread", lambda tid: None
    )  # unregistered
    # normal chat → no handoff noise
    assert ta._handoff_prefix(780, "just dumping some thoughts here") == ""
    # go-signal → generic fallback still fires (safety net)
    assert "may be an" in ta._handoff_prefix(780, "go for it")


def test_registered_thread_always_gets_plan(monkeypatch):
    ta = _adapter()
    monkeypatch.setattr(
        ta, "_handoff_plan_and_auto_start_for_thread", lambda tid: ("SOME_PLAN.md", False)
    )
    pfx = ta._handoff_prefix(2744, "anything at all, even normal chat")
    assert "SOME_PLAN.md" in pfx  # registered handoff unaffected
