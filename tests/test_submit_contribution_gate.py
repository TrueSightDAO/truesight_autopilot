"""Regression tests for the submit_contribution approval gate + execute path.

Guards the 2026-06-18 incident: removing the approval gate (#251) exposed a
latent structural bug — the proposal build + `return json.dumps(proposal)` sat
OUTSIDE `if not approved:`, so `summary` (assigned only inside that branch) was
unbound when `approved=True`, and the execute block was dead code. Result:
every gate-off submit raised `UnboundLocalError: local variable 'summary'
referenced before assignment`, the SSE stream died, and the governor saw
"incomplete chunked read". Fixed in #252.

These tests assert:
- gate OFF (default) -> submit_contribution REACHES the execute path
  (edgar.submit_contribution) and returns success — NOT a crash, NOT pending.
- gate ON + no approval -> returns pending_approval with `summary` bound (no crash).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.config import settings
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


CONTRIB_ARGS = {
    "event_name": "CONTRIBUTION EVENT",
    "attributes": {"Type": "Time (Minutes)", "Amount": "60", "Description": "regression"},
}


class _FakeEdgar:
    """Stand-in for EdgarDirectClient so the execute path makes no network call."""

    def submit_contribution(self, event_name, attributes, description=""):
        return True

    def register_qr_code(self, attributes):
        return True


def _wire(monkeypatch, *, gate: bool):
    monkeypatch.setattr(settings, "require_submission_approval", gate)
    monkeypatch.setattr(m, "EdgarDirectClient", _FakeEdgar)
    # Isolate the gate/execute control flow — label normalization + required-field
    # validation are upstream and have their own tests (test_normalize_submission_labels).
    monkeypatch.setattr(m, "_normalize_submission_labels", lambda event_name, attrs: attrs)
    monkeypatch.setattr(m, "_validate_required_fields", lambda event_name, attrs: [])
    # Allow the WRITE-tool policy gate without loading real governors.
    monkeypatch.setattr(
        m,
        "_resolve_identity",
        lambda display_name=None, **k: SimpleNamespace(
            role=SimpleNamespace(value="governor")
        ),
    )
    monkeypatch.setattr(
        m,
        "_policy_evaluate",
        lambda identity, func_name: SimpleNamespace(allowed=True, reason=""),
    )


def _run(args):
    return asyncio.run(
        m._run_tool(
            "submit_contribution",
            dict(args),
            history=[],
            session_id="tg:1:2",
            governor_name="Gary Teh",
        )
    )


def test_gate_off_executes_and_does_not_crash(monkeypatch):
    """The 2026-06-18 regression: must reach edgar.submit_contribution, not
    raise UnboundLocalError and not return a pending proposal."""
    _wire(monkeypatch, gate=False)
    result = _run(CONTRIB_ARGS)  # asyncio.run re-raises any UnboundLocalError
    low = result.lower()
    assert "submitted successfully" in low, result
    assert "pending_approval" not in low, result


def test_gate_on_returns_pending_with_summary_bound(monkeypatch):
    """Gate on + no approval in history -> pending proposal; `summary` is bound
    (the pending path builds it), so no crash."""
    _wire(monkeypatch, gate=True)
    result = _run(CONTRIB_ARGS)
    assert "pending_approval" in result.lower(), result
