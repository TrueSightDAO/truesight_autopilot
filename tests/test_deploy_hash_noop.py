"""Guard (2026-06-14): deploy_autopilot must NO-OP when already on latest.

Redeploy-loop incident on the vault commit-hash thread (3981): deploy_autopilot
always proceeded to `reset --hard` + restart, which severed in-flight turns; the
adapter then resubmitted the severed message and the model re-called
deploy_autopilot → unbounded loop (plus a tight "deferred / retry when idle"
spin while the long Kopi Bay onboarding turn held the lock).

Fix: a phase-one hash precheck — if deployed HEAD == origin/main, return
status="noop" WITHOUT restarting (and before the idle-drain check).
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())
os.environ["DEPLOY_DRAIN_WAIT_SEC"] = "0"

try:
    import app.main as m
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def _fake_run_local(same_sha: bool):
    def _impl(command, cwd=None, timeout=60):
        if "rev-parse HEAD" in command:
            return "aaaaaaaaaaaa"
        if "rev-parse origin/main" in command:
            return "aaaaaaaaaaaa" if same_sha else "bbbbbbbbbbbb"
        return ""  # git fetch, etc.

    return _impl


def test_noop_when_already_on_latest(monkeypatch):
    """HEAD == origin/main → no restart, even with no busy threads."""
    monkeypatch.setattr(dep, "_is_local", lambda: True)
    monkeypatch.setattr(dep, "_run_local", _fake_run_local(same_sha=True))
    monkeypatch.setattr(m, "_active_streams", {}, raising=False)

    out = json.loads(dep.deploy_autopilot(caller_session="tg:me:1"))
    assert out["status"] == "noop"
    assert out["commit"] == "aaaaaaaaaaaa"
    # crucially: it did NOT proceed to the idle-drain / reset / restart path


def test_proceeds_when_behind(monkeypatch):
    """HEAD != origin/main → precheck does not short-circuit (falls through)."""
    monkeypatch.setattr(dep, "_is_local", lambda: True)
    monkeypatch.setattr(dep, "_run_local", _fake_run_local(same_sha=False))
    # Make a thread look busy so the *next* gate (idle-drain) deterministically
    # returns 'deferred' — proving the precheck let execution fall through.
    import time

    monkeypatch.setattr(
        m, "_active_streams", {"tg:other:2": time.time()}, raising=False
    )
    out = json.loads(dep.deploy_autopilot(caller_session="tg:me:1"))
    assert out["status"] == "deferred"


def test_precheck_failure_does_not_block_deploy(monkeypatch):
    """If the git precheck errors, fall through rather than wedging deploys."""

    def _boom(command, cwd=None, timeout=60):
        raise dep.DeployError("git unavailable")

    monkeypatch.setattr(dep, "_is_local", lambda: True)
    monkeypatch.setattr(dep, "_run_local", _boom)
    import time

    monkeypatch.setattr(
        m, "_active_streams", {"tg:other:2": time.time()}, raising=False
    )
    out = json.loads(dep.deploy_autopilot(caller_session="tg:me:1"))
    # precheck swallowed the error → reached idle-drain → deferred (not noop)
    assert out["status"] == "deferred"
