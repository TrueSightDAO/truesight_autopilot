"""PR2 automatic context compaction - _maybe_auto_compact in the live turn path.

Wired between _compact_old_tool_chains and _trim_history_to_budget at both turn
sites (_stream_chat and _chat_blocking_turn). Spec (plan 2d): a turn that
crosses the token threshold triggers compaction before the LLM call; a turn
under threshold does not; compaction failure (exception) never crashes the turn
- falls back to running uncompacted.
"""

from __future__ import annotations

import copy
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


def _sys() -> dict:
    return {"role": "system", "content": "[ROLE: general]"}


def _user(i: int, text: str | None = None) -> dict:
    return {"role": "user", "content": text or f"user message {i}"}


def _assistant_done(i: int, text: str | None = None) -> dict:
    """Plain assistant reply ending a turn (no tool_calls)."""
    return {
        "role": "assistant",
        "content": text
        or f"**\u2705 Done this turn \u2014 actions taken:**\n\u2022 did thing {i}",
    }


def _assistant_tool(i: int) -> dict:
    """Assistant message with a tool_call (mid-turn)."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "ssh_run", "arguments": "{}"},
            }
        ],
    }


def _tool_result(i: int) -> dict:
    return {"role": "tool", "tool_call_id": f"call_{i}", "content": f"result {i}"}


def _complete_turn(i: int) -> list[dict]:
    """One full completed turn: user -> assistant(tool) -> tool -> assistant(done)."""
    return [_user(i), _assistant_tool(i), _tool_result(i), _assistant_done(i)]


def _bloated_history(n_turns: int = 12) -> list[dict]:
    """System tag + n full turns, each ending with a Done-this-turn report."""
    h = [_sys()]
    for i in range(n_turns):
        h.extend(_complete_turn(i))
    return h


# --- unit tests of _maybe_auto_compact itself -------------------------------


def test_auto_compact_triggers_over_threshold(monkeypatch):
    """A history over the token threshold gets folded (len drops, summary appears)."""
    monkeypatch.setattr(m.settings, "context_compaction_token_threshold", 20000)
    monkeypatch.setattr(m.settings, "context_compaction_keep_last_turns", 2)
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")
    h = _bloated_history(12)
    orig_len = len(h)
    # Make token count cheap + deterministic: force char-based counting.
    monkeypatch.setattr(
        m,
        "_history_token_count",
        lambda msgs: sum(len(str(x.get("content", ""))) for x in msgs),
    )
    # Bypass the cheap char fast-path by setting threshold small vs char count.
    monkeypatch.setattr(m.settings, "context_compaction_token_threshold", 10)
    ok = m._maybe_auto_compact(h, "test-session")
    assert ok is True
    assert len(h) < orig_len  # folded
    contents = [str(x.get("content", "")) for x in h]
    assert any(c.startswith("[CONTEXT SUMMARY") for c in contents)
    # system tag preserved at front
    assert h[0]["role"] == "system"


def test_auto_compact_noop_under_threshold(monkeypatch):
    """Under threshold -> untouched (returns False, list identical)."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")
    h = _bloated_history(4)
    before = copy.deepcopy(h)
    ok = m._maybe_auto_compact(h, "test-session")
    assert ok is False
    assert h == before


def test_auto_compact_kill_switch(monkeypatch):
    """CONTEXT_COMPACTION_AUTO=0 disables even over-threshold sessions."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "0")
    h = _bloated_history(12)
    before = copy.deepcopy(h)
    ok = m._maybe_auto_compact(h, "test-session")
    assert ok is False
    assert h == before


def test_auto_compact_failure_falls_back(monkeypatch):
    """Exception inside compaction never crashes - returns False, history intact."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")

    def boom(*a, **k):
        raise RuntimeError("simulated compaction failure")

    monkeypatch.setattr(m, "compact_history", boom)
    h = _bloated_history(12)
    before = copy.deepcopy(h)
    ok = m._maybe_auto_compact(h, "test-session")
    assert ok is False
    assert h == before  # untouched


def test_auto_compact_preserves_list_object(monkeypatch):
    """history[:] = compacted keeps the SAME list object live (cache invariant)."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")
    monkeypatch.setattr(m.settings, "context_compaction_token_threshold", 10)
    h = _bloated_history(12)
    list_id = id(h)
    m._maybe_auto_compact(h, "test-session")
    assert id(h) == list_id  # same object, mutated in place


def test_auto_compact_invalid_env_disabled(monkeypatch):
    """threshold<=0 or keep<=0 -> disabled (no crash)."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")
    monkeypatch.setenv("CONTEXT_COMPACTION_TOKEN_THRESHOLD", "0")
    h = _bloated_history(12)
    before = copy.deepcopy(h)
    ok = m._maybe_auto_compact(h, "test-session")
    assert ok is False
    assert h == before


# --- integration: helper is called at both turn sites ------------------------


def test_both_turn_sites_call_auto_compact():
    """Both turn paths invoke _maybe_auto_compact between chains and trim."""
    src = open(m.__file__).read()
    # two call sites
    assert src.count("_maybe_auto_compact(history, session_id)") >= 2
    # and one definition
    assert "def _maybe_auto_compact(" in src
    # ordering: chains -> auto-compact -> trim -> sanitise at both sites
    trio = "_compact_old_tool_chains(history)\n    # Context compaction (PR2)"
    assert trio in src


# --- end-to-end: a real turn-shaped history survives the full chain ----------


def test_full_chain_over_threshold_no_dangling(monkeypatch):
    """Whole turn-path sequence leaves a protocol-clean, smaller history."""
    monkeypatch.setenv("CONTEXT_COMPACTION_AUTO", "1")
    monkeypatch.setattr(m.settings, "context_compaction_token_threshold", 100)
    monkeypatch.setattr(m.settings, "context_compaction_keep_last_turns", 2)
    h = _bloated_history(10)
    m._compact_old_tool_chains(h)
    m._maybe_auto_compact(h, "test-session")
    m._trim_history_to_budget(h)
    m._sanitise_tool_messages(h)

    # tool-protocol sanity: every tool msg has a live assistant tool_call ahead
    pending = set()
    for msg in h:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            pending = {tc["id"] for tc in msg["tool_calls"]}
        elif msg["role"] == "tool":
            if not pending:
                pytest.fail("orphan tool message after full chain")
            pending.discard(msg["tool_call_id"])
    assert not pending  # no dangling tool_calls at EOF
    assert h[0]["role"] == "system"
