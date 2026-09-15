"""PR2 — the brain's auto-advance signal (_extract_plan_file +
_compute_advance_signal). Gated on settings.auto_advance; fails closed."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.config import settings
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


PLAN = """\
# Plan

| Unit | Advance | PR opened |
|------|---------|-----------|
| PR1 — parser | `auto` | ☐ |
| PR2 — signal | `auto` | ☐ |
| PR3 — loop | `gate: deploy + UAT` | ☐ |

> **RESUME HERE:** PR2 — the signal.
"""

HANDOFF_MSG = {
    "role": "user",
    "content": (
        "[Handoff context — auto-injected from SOPHIA_HANDOFFS.md: this Telegram "
        "topic (thread 5) is the active handoff for `MY_PLAN.md`. ...]\n\ngo"
    ),
}
OPENED_PR_TRACE = [{"name": "open_fix_pr", "result": "https://github.com/x/y/pull/1"}]


def _write_plan(tmp_path: Path, name: str = "MY_PLAN.md", text: str = PLAN) -> None:
    d = tmp_path / "agentic_ai_context"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


# ── _extract_plan_file (pure) ───────────────────────────────────────────────


def test_extract_plan_file_from_handoff_block():
    assert m._extract_plan_file([HANDOFF_MSG]) == "MY_PLAN.md"


def test_extract_plan_file_none_when_not_handoff():
    assert m._extract_plan_file([{"role": "user", "content": "just chatting"}]) is None


def test_extract_plan_file_takes_latest():
    hist = [
        {"role": "user", "content": "active handoff for `OLD.md`."},
        {"role": "user", "content": "active handoff for `NEW.md`."},
    ]
    assert m._extract_plan_file(hist) == "NEW.md"


# ── _compute_advance_signal (I/O glue) ──────────────────────────────────────


def test_signal_none_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", False)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    assert m._compute_advance_signal([HANDOFF_MSG], OPENED_PR_TRACE) is None


def test_signal_auto_when_pr_opened_and_next_auto(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    sig = m._compute_advance_signal([HANDOFF_MSG], OPENED_PR_TRACE)
    assert sig["decision"] == "auto"
    assert sig["plan"] == "MY_PLAN.md"
    assert sig["next_unit"].startswith("PR2")


def test_signal_gate_when_no_progress_at_all(monkeypatch, tmp_path):
    # An EMPTY tool trace = genuinely nothing happened: no PR *and* no
    # side-effecting action -> still gates (the true non-convergence case).
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    sig = m._compute_advance_signal([HANDOFF_MSG], [])
    assert sig["decision"] == "gate" and "made no progress" in sig["gate_reason"]


def test_signal_auto_when_made_progress_no_pr(monkeypatch, tmp_path):
    # PR-less unit that did a real side-effecting action -> auto-advance. The
    # core PR-less-units fix: progress without a PR no longer gate-locks.
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    trace = [{"name": "git_push_changes", "result": "pushed"}]
    sig = m._compute_advance_signal([HANDOFF_MSG], trace)
    assert sig["decision"] == "auto" and sig["next_unit"].startswith("PR2")


def test_signal_read_only_ssh_run_is_not_progress_no_pr(monkeypatch, tmp_path):
    # ssh_run is read-capable and deliberately excluded: a diagnostics-only turn
    # with no PR must NOT auto-advance (guards the 2026-08-21 bleed fix).
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    trace = [{"name": "ssh_run", "result": "journalctl ..."}]
    sig = m._compute_advance_signal([HANDOFF_MSG], trace)
    assert sig["decision"] == "gate" and "made no progress" in sig["gate_reason"]


def test_signal_gate_when_next_unit_gated(monkeypatch, tmp_path):
    plan = PLAN.replace("**RESUME HERE:** PR2", "**RESUME HERE:** PR3")
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path, text=plan)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    sig = m._compute_advance_signal([HANDOFF_MSG], OPENED_PR_TRACE)
    assert sig["decision"] == "gate" and "deploy" in sig["gate_reason"]


def test_signal_none_when_not_handoff(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    # Non-handoff message → nothing to advance. (The plan-less "normal threads"
    # fallback was REMOVED 2026-08-21: a thread with no plan file never auto-advances,
    # even when it opens a PR — that fallback was the cross-thread bleed root cause.)
    assert m._compute_advance_signal([{"role": "user", "content": "hi"}], []) is None


def test_signal_none_when_plan_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", True)
    # context dir exists but no plan file written -> read fails -> None (closed)
    (tmp_path / "agentic_ai_context").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    assert m._compute_advance_signal([HANDOFF_MSG], OPENED_PR_TRACE) is None


# ---- Cross-thread bleed fixes (2026-08-21) ----


def test_signal_none_when_no_plan_even_if_pr_opened(monkeypatch, tmp_path):
    """A thread with NO plan file must NEVER auto-advance, even when it opened a
    PR. This is the cross-thread bleed fix: without a plan there is no safe
    'next unit' (previously the plan-less fallback emitted
    next_unit='the next PR' which bled into other threads' plans)."""
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(
        tmp_path
    )  # plan file exists in context dir but history has no handoff block
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    sig = m._compute_advance_signal(
        [{"role": "user", "content": "please fix the bug"}], OPENED_PR_TRACE
    )
    assert sig is None


def test_signal_read_only_tools_are_not_progress_in_run_to_uat(monkeypatch, tmp_path):
    """run-to-UAT mode must NOT count read-only tool calls as progress; only
    real UAT/test tooling. Previously bool(tool_trace) made any chat turn with
    an ssh_run/read auto-advance."""
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_until_uat", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    trace = [
        {"name": "ssh_run", "result": "ok"},
        {"name": "read_repo_file", "result": "..."},
    ]
    sig = m._compute_advance_signal([HANDOFF_MSG], trace)
    # plan file present + handoff, but no real progress -> gate, NOT auto
    assert sig is None or sig["decision"] == "gate"


def test_signal_uat_tools_are_progress_in_run_to_uat(monkeypatch, tmp_path):
    """run-to-UAT mode counts UAT/test tooling (extract_pdf_text, run_tests) as
    progress so UAT units still auto-advance, without treating read-only lookups
    as progress."""
    monkeypatch.setattr(settings, "auto_advance", True)
    monkeypatch.setattr(settings, "auto_advance_until_uat", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    trace = [{"name": "extract_pdf_text", "result": "..."}]
    sig = m._compute_advance_signal([HANDOFF_MSG], trace)
    assert sig is not None and sig["decision"] == "auto"
    assert sig["plan"] == "MY_PLAN.md"
