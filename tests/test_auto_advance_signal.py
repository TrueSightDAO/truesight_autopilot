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


def test_signal_gate_when_no_pr_opened(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", True)
    _write_plan(tmp_path)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    sig = m._compute_advance_signal([HANDOFF_MSG], [])  # no open_fix_pr
    assert sig["decision"] == "gate" and "did not open a PR" in sig["gate_reason"]


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
    # Non-handoff message + NO PR opened → nothing to advance. (Since #268 a non-handoff
    # message that DID open a PR auto-advances via the "normal threads" fallback, so the
    # no-PR trace is what exercises the None path here.)
    assert m._compute_advance_signal([{"role": "user", "content": "hi"}], []) is None


def test_signal_none_when_plan_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "auto_advance", True)
    # context dir exists but no plan file written -> read fails -> None (closed)
    (tmp_path / "agentic_ai_context").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "context_repos_dir", tmp_path)
    assert m._compute_advance_signal([HANDOFF_MSG], OPENED_PR_TRACE) is None
