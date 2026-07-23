"""Regression tests: /chat-blocking convergence + DSML-leak parity with the
streaming path (2026-07-23).

Root-caused via a real governor-signed ping_sophia call (2026-07-21) that came
back garbled. /chat-blocking had its own copy of "force a clean completion if
the round budget runs out," written before app/turn_convergence.py existed,
and never got either follow-up hardening the streaming path (_run_tool_round_loop)
already has:

  1. The soft-budget convergence nudge (should_converge / convergence_message)
     that tells the model to wind down and write a clean, resumable answer a
     few rounds BEFORE the hard cap, instead of grinding into it.
  2. _strip_dsml / _ensure_nonempty_final, which catch DeepSeek's raw
     "<｜｜DSML｜｜...>" token leak — the blocking path's own guard only matched
     the literal substring "<tool_call>", a different (and, in the incident,
     absent) leak signature, so the leaked text passed straight through.

These tests exercise _chat_blocking_turn directly (the testable unit inside
the /chat-blocking route) with a fake LLMClient, so no network/DeepSeek calls
are made.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.roles import ROLES
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


def _wire_common(monkeypatch):
    """Bypass session/role plumbing unrelated to the loop under test —
    mirrors tests/test_chat_session_lock_ordering.py's _wire_common."""
    monkeypatch.setattr(m, "find_role_in_history", lambda history: ROLES["general"])
    monkeypatch.setattr(m, "_gov_name_for_key", lambda pk: None)
    monkeypatch.setattr(m, "_load_or_create_session", lambda sid: [])
    monkeypatch.setattr(m, "_log_session", lambda sid, history: None)
    monkeypatch.setattr(m, "_sanitise_tool_messages", lambda history: None)
    monkeypatch.setattr(m, "_append_turn_report", lambda text, state: text)
    monkeypatch.setattr(m, "_compute_advance_signal", lambda history, trace: None)

    async def _fake_run_tool(func_name, func_args, history, session_id, gov_name):
        return {"ok": True, "tool": func_name}

    monkeypatch.setattr(m, "_run_tool", _fake_run_tool)


def _tool_call_completion(call_id: str = "call_1") -> dict:
    """A completion whose message carries one tool call (loop keeps going)."""
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "noop_tool", "arguments": "{}"},
                        }
                    ],
                }
            }
        ]
    }


def _text_completion(text: str) -> dict:
    """A completion with a final text answer, no tool calls."""
    return {"choices": [{"message": {"content": text, "tool_calls": []}}]}


class _FakeLLMClient:
    """Replaces app.main.LLMClient. `responses` is consumed in order by
    successive .chat() calls; the last entry repeats once exhausted."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, system_prompt, history, tools=None):
        self.calls += 1
        if self._responses:
            resp = self._responses.pop(0)
        else:
            resp = self._responses_last
        self._responses_last = resp
        return resp

    def extract_text(self, completion: dict) -> str:
        return completion["choices"][0]["message"].get("content", "") or ""

    def extract_tool_calls(self, completion: dict) -> list:
        return completion["choices"][0]["message"].get("tool_calls", []) or []


def _run_turn(monkeypatch, fake_client, max_rounds: int = 6):
    monkeypatch.setenv("CHAT_BLOCKING_MAX_ROUNDS", str(max_rounds))
    monkeypatch.setattr(m, "LLMClient", lambda: fake_client)
    return asyncio.run(
        m._chat_blocking_turn("test-session", "do the multi-step thing", "pubkey")
    )


class TestConvergenceNudge:
    def test_soft_budget_directive_injected_before_hard_cap(self, monkeypatch):
        """A turn that never naturally converges must get the soft-budget
        wind-down directive appended to history before exhausting max_rounds
        — the exact gap that let the 2026-07-21 turn grind silently."""
        _wire_common(monkeypatch)
        max_rounds = 6
        # Always return a tool call — the model "never" produces a final
        # answer on its own, forcing the loop to run every round.
        responses = [_tool_call_completion(f"call_{i}") for i in range(max_rounds)]
        fake_client = _FakeLLMClient(responses)

        history: list[dict] = []
        monkeypatch.setattr(m, "_load_or_create_session", lambda sid: history)

        resp = _run_turn(monkeypatch, fake_client, max_rounds=max_rounds)
        assert resp.status_code == 200

        nudges = [
            msg
            for msg in history
            if msg.get("role") == "user" and "RESUME HERE" in str(msg.get("content", ""))
        ]
        assert len(nudges) == 1, (
            "expected exactly one convergence directive injected into history; "
            f"history={history}"
        )
        # soft_budget(6, 0.75) == 5 (see test_turn_convergence.py) — must fire
        # strictly before the hard cap, not after grinding through all 6 rounds.
        assert "5 of 6" in nudges[0]["content"]

    def test_short_turn_with_no_tool_calls_is_unaffected(self, monkeypatch):
        """Regression guard: a normal single-round answer must still work
        exactly as before — the fix must not touch the common case."""
        _wire_common(monkeypatch)
        fake_client = _FakeLLMClient([_text_completion("All done, here's the answer.")])

        resp = _run_turn(monkeypatch, fake_client, max_rounds=15)
        assert resp.status_code == 200
        assert resp.body is not None
        import json as _json

        data = _json.loads(resp.body)
        assert data["response"] == "All done, here's the answer."
        assert fake_client.calls == 1  # no retry needed


class TestDsmlLeakParity:
    def test_dsml_leak_is_stripped_and_retried_not_passed_through(self, monkeypatch):
        """The blocking path's old guard only matched the literal substring
        "<tool_call>" — DeepSeek's actual leak format ("<｜｜DSML｜｜...>") slipped
        straight through to the caller. Must now be caught and retried, same
        as the streaming path. Uses a properly closed tool_calls block (the
        real shape observed 2026-07-21), which _strip_dsml removes whole —
        leaving nothing behind, so the retry path must fire."""
        _wire_common(monkeypatch)
        leaked = (
            "<｜｜DSML｜｜tool_calls>\n"
            "<｜｜DSML｜｜invoke name=\"read_tool_result\">\n"
            "<｜｜DSML｜｜parameter name=\"artifact_id\">call_00_abc</｜｜DSML｜｜parameter>\n"
            "</｜｜DSML｜｜invoke>\n"
            "</｜｜DSML｜｜tool_calls>"
        )
        clean = "Here is the clean, resumable answer."
        fake_client = _FakeLLMClient(
            [_text_completion(leaked), _text_completion(clean)]
        )

        resp = _run_turn(monkeypatch, fake_client, max_rounds=15)
        assert resp.status_code == 200
        import json as _json

        data = _json.loads(resp.body)
        assert data["response"] == clean
        assert "DSML" not in data["response"]
        assert fake_client.calls == 2  # first (leaked) + forced retry

    def test_old_literal_tool_call_leak_still_caught(self, monkeypatch):
        """Keep the pre-existing "<tool_call>" text-leak guard working too —
        this fix should be additive, not a replacement that drops coverage."""
        _wire_common(monkeypatch)
        leaked = "<tool_call>{\"name\": \"foo\", \"arguments\": {}}</tool_call>"
        clean = "Clean retry text."
        fake_client = _FakeLLMClient(
            [_text_completion(leaked), _text_completion(clean)]
        )

        resp = _run_turn(monkeypatch, fake_client, max_rounds=15)
        assert resp.status_code == 200
        import json as _json

        data = _json.loads(resp.body)
        assert data["response"] == clean
        assert fake_client.calls == 2

    def test_blank_completion_is_forced_and_falls_back(self, monkeypatch):
        """If even the forced retry comes back blank, fall back to the fixed
        non-empty message rather than ever returning an empty response."""
        _wire_common(monkeypatch)
        fake_client = _FakeLLMClient(
            [_text_completion(""), _text_completion("")]
        )

        resp = _run_turn(monkeypatch, fake_client, max_rounds=15)
        assert resp.status_code == 200
        import json as _json

        data = _json.loads(resp.body)
        assert data["response"].strip() != ""
