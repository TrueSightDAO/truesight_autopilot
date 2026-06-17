"""PR3 — the adapter's auto-advance loop (_run_turn_with_auto_advance).

Drives the loop with a scripted fake brain (call_chat_with_progress) that
populates the advance_out box, and asserts the loop continues / pauses / stops
exactly per the brain's decisions. With AUTO_ADVANCE off it must run a single
turn (unchanged behavior)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    import app.telegram_adapter as ta
    from app.config import settings
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.telegram_adapter import unavailable: {exc}", allow_module_level=True)


def _wire(monkeypatch, scripts):
    """Patch send_message/voice and a scripted call_chat_with_progress.
    Returns (sent_messages, call_counter)."""
    sent: list[str] = []
    monkeypatch.setattr(
        ta, "send_message", lambda c, text, th=None: sent.append(text)
    )
    monkeypatch.setattr(ta, "_handle_voice_reply", lambda *a, **k: None)

    counter = {"n": 0}

    def fake_ccwp(chat_id, thread_id, message, session_id, public_key, *, advance_out=None):
        i = counter["n"]
        counter["n"] += 1
        if advance_out is not None and i < len(scripts):
            advance_out["advance"] = scripts[i]
        return (f"response {i}", True)

    monkeypatch.setattr(ta, "call_chat_with_progress", fake_ccwp)
    return sent, counter


def test_off_runs_single_turn(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", False)
    sent, counter = _wire(
        monkeypatch, [{"decision": "auto", "next_unit": "PR2", "plan": "P.md"}]
    )
    ta._run_turn_with_auto_advance(-100, 5, "go", "tg:-100:5", "PK", False, None)
    assert counter["n"] == 1  # exactly one turn despite an auto signal
    assert not any("Continuing" in s for s in sent)


def test_runs_until_gate(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_max_turns", 8)
    scripts = [
        {"decision": "auto", "next_unit": "PR2", "plan": "P.md"},
        {"decision": "auto", "next_unit": "PR3", "plan": "P.md"},
        {"decision": "gate", "next_unit": "PR3", "gate_reason": "deploy first", "plan": "P.md"},
    ]
    sent, counter = _wire(monkeypatch, scripts)
    ta._run_turn_with_auto_advance(-100, 6, "go", "tg:-100:6", "PK", False, None)
    assert counter["n"] == 3
    assert sum("Continuing to" in s for s in sent) == 2
    assert any("Paused before" in s and "deploy first" in s for s in sent)


def test_stops_on_done(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_max_turns", 8)
    scripts = [
        {"decision": "auto", "next_unit": "PR2", "plan": "P.md"},
        {"decision": "done"},
    ]
    sent, counter = _wire(monkeypatch, scripts)
    ta._run_turn_with_auto_advance(-100, 7, "go", "tg:-100:7", "PK", False, None)
    assert counter["n"] == 2
    assert any("Plan complete" in s for s in sent)


def test_respects_cap(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_max_turns", 2)
    # Always-auto plan: would loop forever without the cap.
    scripts = [{"decision": "auto", "next_unit": f"PR{i}", "plan": "P.md"} for i in range(10)]
    sent, counter = _wire(monkeypatch, scripts)
    ta._run_turn_with_auto_advance(-100, 8, "go", "tg:-100:8", "PK", False, None)
    # turn0 + 2 auto-continued turns = 3 calls, then cap message
    assert counter["n"] == 3
    assert any("auto-advance cap" in s for s in sent)


def test_gate_first_turn_no_continue(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_max_turns", 8)
    scripts = [{"decision": "gate", "next_unit": "PR1", "gate_reason": "needs merge", "plan": "P.md"}]
    sent, counter = _wire(monkeypatch, scripts)
    ta._run_turn_with_auto_advance(-100, 9, "go", "tg:-100:9", "PK", False, None)
    assert counter["n"] == 1
    assert any("needs merge" in s for s in sent)
    assert not any("Continuing" in s for s in sent)


def test_unknown_decision_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_max_turns", 8)
    scripts = [{"decision": "???", "next_unit": "PR2", "plan": "P.md"}]
    sent, counter = _wire(monkeypatch, scripts)
    ta._run_turn_with_auto_advance(-100, 10, "go", "tg:-100:10", "PK", False, None)
    assert counter["n"] == 1  # stops, does not continue on an unknown decision
