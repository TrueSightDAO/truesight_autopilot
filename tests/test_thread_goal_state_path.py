"""Regression tests: the thread-goal state file must not depend on the CWD.

A bare ``data/thread_goals.jsonl`` used to resolve against whatever directory a
process happened to start in, so the web server, the loop daemon and a one-off
CLI/pytest run could each read and write a DIFFERENT file. A goal set by one
process then looked absent to the other -- the ``lost_goal`` false alarm, and
the "nothing was set for this thread -- nothing to complete" false negative.
"""

from __future__ import annotations

from app import thread_goal as tg
from app.tools import thread_goal_tools as tools


def test_state_path_is_absolute_and_repo_anchored(monkeypatch):
    """With no env override the state file anchors on the repo root, absolutely."""
    monkeypatch.delenv("THREAD_GOAL_STATE_DIR", raising=False)
    monkeypatch.delenv("DEPLOY_WATCHER_STATE_DIR", raising=False)
    path = tg._state_path()
    assert path.is_absolute()
    assert path == tg._REPO_ROOT / "data" / tg._STATE_FILENAME


def test_state_path_is_cwd_independent(monkeypatch, tmp_path):
    """Two processes with different CWDs must resolve the SAME state file."""
    monkeypatch.delenv("THREAD_GOAL_STATE_DIR", raising=False)
    monkeypatch.delenv("DEPLOY_WATCHER_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    from_elsewhere = tg._state_path()
    monkeypatch.chdir(tg._REPO_ROOT)
    from_root = tg._state_path()
    assert from_elsewhere == from_root
    assert from_elsewhere.is_absolute()


def test_state_path_honours_absolute_env_override(monkeypatch, tmp_path):
    """An absolute THREAD_GOAL_STATE_DIR is still used verbatim (tests/ops)."""
    monkeypatch.setenv("THREAD_GOAL_STATE_DIR", str(tmp_path))
    assert tg._state_path() == tmp_path / tg._STATE_FILENAME


def test_explicit_sid_only_used_when_context_has_none(monkeypatch):
    """Rule 5: context beats an explicit session_id, so no cross-thread bleed."""
    spec = next(s for s in tools.TOOL_SPECS if s.name == "complete_thread_goal")
    monkeypatch.setattr(
        tools, "complete_thread_goal", lambda session_id=None: session_id
    )
    assert spec.handler({}, {"session_id": "ctx-sid"}) == "ctx-sid"
    assert spec.handler({"session_id": "named-sid"}, {}) == "named-sid"
    assert spec.handler({"session_id": "   "}, {}) is None


def test_dismiss_accepts_optional_explicit_sid(monkeypatch):
    """dismiss_thread_goal exposes session_id too, and respects context first."""
    spec = next(s for s in tools.TOOL_SPECS if s.name == "dismiss_thread_goal")
    assert "session_id" in spec.parameters["properties"]
    assert spec.parameters.get("required") == []
    seen: dict[str, str | None] = {}

    def fake_dismiss(reason: str = "", session_id: str | None = None) -> str:
        seen["reason"] = reason
        seen["session_id"] = session_id
        return "ok"

    monkeypatch.setattr(tools, "dismiss_thread_goal", fake_dismiss)
    spec.handler({"reason": "superseded"}, {"session_id": "ctx-sid"})
    assert seen == {"reason": "superseded", "session_id": "ctx-sid"}
    spec.handler({"reason": "x", "session_id": "named"}, {})
    assert seen == {"reason": "x", "session_id": "named"}
