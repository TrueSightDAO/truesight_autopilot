"""Tests for registry-first auto-advance plan scoping (2026-09-25).

Chained parks after a PR: the old _extract_plan_file() last-wins history scan
lost the injected handoff prefix after compaction (plan_file=None -> no advance
signal -> thread silently stopped) and could be hijacked by any later turn
quoting a different plan's filename (wrong-plan bleed). _resolve_plan_for_signals
now resolves thread_id -> plan via HANDOFF_MANIFEST.md first, falling back to the
history literal only when the registry yields nothing.
"""

import app.main as m
import app.telegram_adapter as ta


def test_thread_id_from_session_id_telegram():
    assert m._thread_id_from_session_id("tg:-1003919341801:35765") == 35765


def test_thread_id_from_session_id_non_telegram():
    assert m._thread_id_from_session_id("dc:123:456") is None
    assert m._thread_id_from_session_id(None) is None
    assert m._thread_id_from_session_id("garbage") is None


def test_registry_first_wins_when_history_has_no_literal(monkeypatch):
    monkeypatch.setattr(
        ta,
        "_handoff_plan_for_thread",
        lambda tid: "plans/SCHWAB_PORTFOLIO_INTEGRATION_PLAN.md",
    )
    hist = [{"role": "user", "content": "[CONTEXT SUMMARY] compacted, no prefix here"}]
    assert (
        m._resolve_plan_for_signals(hist, "tg:-1003919341801:35765")
        == "plans/SCHWAB_PORTFOLIO_INTEGRATION_PLAN.md"
    )


def test_registry_wins_over_stale_history_literal(monkeypatch):
    monkeypatch.setattr(
        ta,
        "_handoff_plan_for_thread",
        lambda tid: "plans/SCHWAB_PORTFOLIO_INTEGRATION_PLAN.md",
    )
    hist = [{"role": "user", "content": "the active handoff for `WRONG_PLAN.md`."}]
    assert (
        m._resolve_plan_for_signals(hist, "tg:-1003919341801:35765")
        == "plans/SCHWAB_PORTFOLIO_INTEGRATION_PLAN.md"
    )


def test_falls_back_to_history_literal_when_unregistered(monkeypatch):
    monkeypatch.setattr(ta, "_handoff_plan_for_thread", lambda tid: None)
    hist = [
        {
            "role": "user",
            "content": "the active handoff for `SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md`.",
        }
    ]
    assert (
        m._resolve_plan_for_signals(hist, "tg:-1003919341801:36801")
        == "SOPHIA_GOAL_LOOP_AND_BRAIN_PLAN.md"
    )


def test_non_telegram_session_uses_history_literal(monkeypatch):
    called = {"n": 0}

    def boom(tid):
        called["n"] += 1
        return "SHOULD_NOT_BE_USED.md"

    monkeypatch.setattr(ta, "_handoff_plan_for_thread", boom)
    hist = [{"role": "user", "content": "the active handoff for `X.md`."}]
    assert m._resolve_plan_for_signals(hist, "dc:1:2") == "X.md"
    assert called["n"] == 0
