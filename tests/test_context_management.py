"""Context-management pillars A (tool-result externalization) + B (sub-task compaction).

Stops the tool-result context-poisoning bricks: big results go to a local artifact
file (recoverable via read_tool_result), and completed tool-chains compact to a
one-line note so the token-trim has to drop far less.
"""

from __future__ import annotations

import copy
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.tools import artifact_tools as at
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


# ── Pillar A: externalization ─────────────────────────────────────────────────


def test_externalize_small_unchanged():
    assert m._externalize_tool_result("ok", "tc1", "sess") == "ok"


def test_externalize_large_writes_artifact_and_is_recoverable(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_ARTIFACT_DIR", tmp_path / "artifacts")
    big = "Z" * 50_000
    out = m._externalize_tool_result(big, "tc-abc", "tg:1:2", "ssh_run")
    assert out.startswith("Z" * 8000)
    assert "saved to artifact 'tc-abc'" in out and "read_tool_result" in out
    # full content recoverable (read with a limit above the size so no paging)
    assert m._read_artifact("tc-abc", "tg:1:2", 0, 100_000) == big
    # default limit pages a very large artifact (16K + a pager note)
    paged = m._read_artifact("tc-abc", "tg:1:2")
    assert paged.startswith("Z" * 16_000) and "more chars" in paged


def test_externalize_flag_off_falls_back_to_truncation(monkeypatch):
    monkeypatch.setenv("CONTEXT_EXTERNALIZE", "0")
    out = m._externalize_tool_result("Z" * 50_000, "tc1", "sess")
    assert "truncated" in out and "artifact" not in out


def test_read_artifact_missing_is_graceful():
    assert "not found" in m._read_artifact("nope", "sess-x")


def test_read_tool_result_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_ARTIFACT_DIR", tmp_path / "artifacts")
    m._externalize_tool_result("B" * 40_000, "tcX", "tg:9:9", "ssh_run")
    ok = at.TOOL_SPEC.handler({"artifact_id": "tcX"}, {"session_id": "tg:9:9"})
    assert ok["status"] == "ok" and ok["content"].startswith("B")
    assert (
        at.TOOL_SPEC.handler({"artifact_id": "tcX"}, {})["status"] == "error"
    )  # no session


# ── Pillar B: compaction ──────────────────────────────────────────────────────


def test_compact_collapses_old_tool_chains(monkeypatch):
    monkeypatch.setenv("CONTEXT_COMPACT", "1")
    monkeypatch.setenv("CONTEXT_COMPACT_KEEP_RECENT", "2")
    history = [
        {"role": "user", "content": "do X"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "ssh_run"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result1"},
        {"role": "assistant", "content": "done X"},
        {"role": "user", "content": "now Y"},  # within keep_recent
        {"role": "assistant", "content": "working Y"},  # within keep_recent
    ]
    m._compact_old_tool_chains(history)
    roles = [x["role"] for x in history]
    assert "tool" not in roles  # old tool machinery gone
    assert any("compacted" in (x.get("content") or "") for x in history)
    assert history[-1]["content"] == "working Y" and history[-2]["content"] == "now Y"


def test_compact_keeps_recent_active_chain_untouched(monkeypatch):
    monkeypatch.setenv("CONTEXT_COMPACT", "1")
    monkeypatch.setenv("CONTEXT_COMPACT_KEEP_RECENT", "10")
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "x"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]
    before = copy.deepcopy(history)
    m._compact_old_tool_chains(history)
    assert history == before  # under keep_recent → untouched


def test_compact_flag_off_noop(monkeypatch):
    monkeypatch.setenv("CONTEXT_COMPACT", "0")
    monkeypatch.setenv("CONTEXT_COMPACT_KEEP_RECENT", "0")
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "x"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "user", "content": "u"},
    ]
    before = copy.deepcopy(history)
    m._compact_old_tool_chains(history)
    assert history == before


def test_read_tool_result_is_universal_across_roles():
    # The recovery tool must be offered to every role, not just the catch-all.
    from app.roles import ROLES, get_tool_schemas_for_role

    specialised = [r for r in ROLES.values() if r.tools]
    assert specialised, "expected at least one role with an explicit tool list"
    for r in specialised:
        names = {t["function"]["name"] for t in get_tool_schemas_for_role(r)}
        assert "read_tool_result" in names, f"{r.key} missing read_tool_result"
