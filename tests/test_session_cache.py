"""Regression test: _log_session must keep the in-memory _sessions cache in sync.

Bug (role menu re-prompt loop): a new topic does `history = build_role_menu()`
(reassigns the local var) then `_log_session` writes only to disk. The in-memory
`_sessions[session_id]` still pointed at the original empty list, so the next
message re-loaded stale empty history → `len(history)==0` → role menu re-prompted
forever (replying "1" never stuck).
"""

from __future__ import annotations

import os
import tempfile

import pytest

# app.main has filesystem import side-effects (creates CONTEXT_REPOS_DIR, default
# /opt/truesight_autopilot/...). Redirect to tmp before import; skip gracefully if
# config was already imported with the prod path elsewhere in the suite.
os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.roles import build_role_menu
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.main import unavailable in this env: {exc}", allow_module_level=True
    )


def test_log_session_syncs_in_memory_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "SESSION_LOG_DIR", tmp_path)
    sid = "tg:unit-test:0"
    m._sessions.pop(sid, None)

    # Message 1: brand-new topic → empty history, cached in _sessions.
    h1 = m._load_or_create_session(sid)
    assert h1 == []

    # Role menu replaces the local history and persists it.
    menu = build_role_menu()
    assert len(menu) > 0
    m._log_session(sid, menu)

    # Message 2 must NOT see stale empty history (the bug). With the fix,
    # _log_session synced _sessions[sid] = menu, so the role flow proceeds.
    h2 = m._load_or_create_session(sid)
    assert len(h2) > 0, "stale empty history — role menu would re-prompt"
    assert h2 == menu

    m._sessions.pop(sid, None)
