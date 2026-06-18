"""Guard #2 (2026-06-12): stop Sophia self-bricking.

(A) deploy_autopilot DEFERS instead of restarting while another thread is mid-turn
    (a restart severs in-flight turns + wedges the adapter).
(B) tool results are truncated before entering the transcript, so big/binary
    outputs can't bloat a thread's context into an empty-response poison.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())
os.environ["DEPLOY_DRAIN_WAIT_SEC"] = "0"  # no drain wait in tests

try:
    import app.main as m
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


# ── (B) tool-result truncation ────────────────────────────────────────────────


def test_truncate_passes_short_results():
    assert m._truncate_tool_result("ok") == "ok"
    assert m._truncate_tool_result(None) is None


def test_truncate_caps_big_results():
    big = "x" * 50_000
    out = m._truncate_tool_result(big)
    assert len(out) < 9_000
    assert "truncated" in out
    assert out.startswith("x" * 8000)


# ── (A) idle-drain guard ──────────────────────────────────────────────────────


def test_other_threads_busy_excludes_caller_and_stale(monkeypatch):
    import time

    now = time.time()
    monkeypatch.setattr(
        m,
        "_active_streams",
        {"tg:me:1": now, "tg:other:2": now, "tg:stale:3": now - 9999},
        raising=False,
    )
    busy = dep._other_threads_busy(caller_session="tg:me:1")
    assert busy == ["tg:other:2"]  # caller excluded, stale excluded


def test_deploy_defers_when_a_thread_is_busy(monkeypatch):
    import time

    # Mock the git hash check so it returns different SHAs (passes the check
    # and reaches the idle-drain guard instead of short-circuiting with noop)
    monkeypatch.setattr(dep, "_run_local", lambda cmd, cwd, timeout: (
        "abc123" if "rev-parse HEAD" in cmd else "def456"
    ))
    monkeypatch.setattr(dep, "_is_process_stale", lambda rd: False)

    monkeypatch.setattr(
        m, "_active_streams", {"tg:other:2": time.time()}, raising=False
    )
    out = json.loads(dep.deploy_autopilot(caller_session="tg:me:1"))
    assert out["status"] == "deferred"
    assert "tg:other:2" in out["busy_threads"]
    # crucially: it returned WITHOUT attempting a deploy/restart


def test_idle_means_no_busy_threads(monkeypatch):
    monkeypatch.setattr(m, "_active_streams", {}, raising=False)
    assert dep._other_threads_busy(caller_session="tg:me:1") == []
