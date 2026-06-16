"""PR0 — safety net: a concurrent-write race must never brick a thread.

Covers both tool-protocol corruption directions that DeepSeek rejects, plus the
atomic transcript write. These are the regression tests for the 2026-06-10
incident where Telegram topics 'Stream of consciousness' (thread 780) and
'Digital Infrastructure' (thread 3) 400-ed on every reply because a raced write
left an assistant `tool_calls` message with no following `tool` results.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.main import unavailable in this env: {exc}", allow_module_level=True
    )


def _assistant(call_ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "do_thing", "arguments": "{}"},
            }
            for cid in call_ids
        ],
    }


def _tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _dangling_indices(history):
    """Return indices of assistant tool_calls msgs lacking a following tool result."""
    bad = []
    n = len(history)
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            ids = {tc["id"] for tc in msg["tool_calls"]}
            seen = set()
            j = i + 1
            while j < n and history[j].get("role") == "tool":
                seen.add(history[j].get("tool_call_id"))
                j += 1
            if not ids.issubset(seen):
                bad.append(i)
    return bad


# ── Pass 2: heal orphan tool_calls (the bug that actually bricked prod) ────────


def test_heals_single_orphan_tool_call():
    history = [
        {"role": "user", "content": "hi"},
        _assistant(["call_A"]),  # no following tool result
        {"role": "user", "content": "next message landed in the gap"},
    ]
    m._sanitise_tool_messages(history)
    assert _dangling_indices(history) == []
    # a synthetic tool result was injected directly after the assistant
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "call_A"
    assert history[2]["content"] == m._RECOVERED_TOOL_RESULT


def test_heals_partial_multi_call():
    """Assistant made 2 calls, only the first got a result (the thread-3 shape)."""
    history = [
        _assistant(["call_0", "call_1"]),
        _tool("call_0", "first ok"),
        {"role": "assistant", "content": "done"},
    ]
    m._sanitise_tool_messages(history)
    assert _dangling_indices(history) == []
    # real result preserved, synthetic injected for the missing one, both contiguous
    assert history[1] == _tool("call_0", "first ok")
    assert history[2]["role"] == "tool" and history[2]["tool_call_id"] == "call_1"
    assert history[3]["role"] == "assistant"


def test_heals_trailing_orphan_tool_call():
    history = [{"role": "user", "content": "go"}, _assistant(["call_Z"])]
    m._sanitise_tool_messages(history)
    assert _dangling_indices(history) == []
    assert history[-1]["role"] == "tool" and history[-1]["tool_call_id"] == "call_Z"


# ── Pass 1: orphan tool messages (pre-existing behavior, must still hold) ──────


def test_drops_orphan_tool_message():
    history = [
        {"role": "user", "content": "hi"},
        _tool("ghost", "result with no owner"),
        {"role": "assistant", "content": "reply"},
    ]
    m._sanitise_tool_messages(history)
    assert all(msg.get("role") != "tool" for msg in history)
    assert len(history) == 2


def test_wellformed_history_untouched():
    history = [
        {"role": "user", "content": "hi"},
        _assistant(["call_A"]),
        _tool("call_A", "real"),
        {"role": "assistant", "content": "done"},
    ]
    before = json.dumps(history)
    m._sanitise_tool_messages(history)
    assert json.dumps(history) == before


def _tool_messages_well_anchored(history):
    """DeepSeek invariant: every `tool` msg must sit in a contiguous run that begins
    immediately after an assistant message carrying `tool_calls`."""
    for i, msg in enumerate(history):
        if msg.get("role") == "tool":
            j = i - 1
            while j >= 0 and history[j].get("role") == "tool":
                j -= 1
            if (
                j < 0
                or history[j].get("role") != "assistant"
                or not history[j].get("tool_calls")
            ):
                return False
    return True


def test_drops_nonadjacent_tool_message_thread5712():
    """The 2026-06-16 brick: a tool result whose owning assistant `tool_calls` exists
    but is separated by a `user` message. DeepSeek 400s ("must be a response to a
    preceding message with tool_calls"). The old "id known anywhere" Pass 1 kept it;
    adjacency-aware Pass 1 drops it and Pass 2 re-anchors a synthetic result."""
    history = [
        {"role": "user", "content": "go"},
        _assistant(["call_X"]),
        {"role": "user", "content": "status?"},  # breaks adjacency
        _tool("call_X", "late result"),
    ]
    m._sanitise_tool_messages(history)
    assert _tool_messages_well_anchored(history), history
    assert _dangling_indices(history) == []


def test_multi_tool_run_after_toolcalls_preserved():
    """A contiguous multi-result run immediately after the assistant is valid and
    must survive adjacency-aware Pass 1 unchanged."""
    history = [
        _assistant(["c0", "c1"]),
        _tool("c0", "r0"),
        _tool("c1", "r1"),
        {"role": "assistant", "content": "done"},
    ]
    before = json.dumps(history)
    m._sanitise_tool_messages(history)
    assert json.dumps(history) == before
    assert _tool_messages_well_anchored(history)


# ── 0c: atomic write + 0b: healing on load ────────────────────────────────────


def test_log_session_atomic_and_load_heals(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "SESSION_LOG_DIR", tmp_path)
    sid = "tg:-100:999"
    m._sessions.pop(sid, None)

    # Persist a poisoned history (assistant tool_calls + immediate user msg).
    poisoned = [
        {"role": "user", "content": "do it"},
        _assistant(["call_X"]),
        {"role": "user", "content": "and another"},
    ]
    m._log_session(sid, poisoned)
    # no leftover .tmp files
    assert not list(tmp_path.glob("*.tmp"))

    # Drop the in-memory copy so the next load reads from disk and heals.
    m._sessions.pop(sid, None)
    loaded = m._load_or_create_session(sid)
    assert _dangling_indices(loaded) == [], "load must heal a raced transcript"
    m._sessions.pop(sid, None)
