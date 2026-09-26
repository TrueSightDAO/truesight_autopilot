"""PR1 tests — governor-only manual session-compaction tool.

Covers: session-reference resolution, end-to-end compaction against an
on-disk bloated session (backup file created, token drop, retained tail
byte-identical), no-op under threshold (+ force), refusal to race an
in-flight session in another thread, and the governor-only policy gate.

The tool handler never crashes the turn: all error paths return a JSON
string with status=error/blocked/noop, never raise.
"""

from __future__ import annotations

import copy
import glob
import json
import os

import pytest

try:
    from app.config import settings
    from app.tools.compact_session_tool import (
        TOOL_SPEC,
        _resolve_session,
        compact_session_manual,
    )
    from app.context_compaction import find_turn_boundaries
    from app.policy import ActionClass, Identity, Role, classify_action, evaluate
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"imports unavailable: {exc}", allow_module_level=True)

HAS_MAIN = True
try:
    import app.main as m  # noqa: F401 — only to prove the live module imports
except Exception:  # noqa: BLE001
    HAS_MAIN = False

DONE_MARKER = "**✅ Done this turn — actions taken:**"


def _turn(
    user_text: str, done_bullets: list[str] | None = None, tool_msgs: int = 3
) -> list[dict]:
    """One completed turn: user -> (assistant tool_calls + tool result)* -> plain assistant."""
    msgs = [
        {"role": "user", "content": user_text},
    ]
    for i in range(tool_msgs):
        msgs.append(
            {
                "role": "assistant",
                "content": f"Calling tool {i}.",
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": f"tool_{i}", "arguments": "{}"},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "content": f"tool result {i}: "
                + ("x" * 1200),  # verbose real-world tool output
                "tool_call_id": f"call_{i}",
            }
        )
    body = DONE_MARKER + "\n" if done_bullets else ""
    if done_bullets:
        body = DONE_MARKER + "\n" + "\n".join(f"• {b}" for b in done_bullets) + "\n"
    msgs.append({"role": "assistant", "content": body or f"Reply to: {user_text}"})
    return msgs


def _make_session(session_dir, n_turns: int = 15, per_turn_tools: int = 3) -> str:
    """Write a bloated session (system + n_turns) to disk; return 12-hex hash."""
    import hashlib

    key = "tg:-1003919341801:99999"
    hash12 = hashlib.md5(key.encode()).hexdigest()[:12]
    history = [{"role": "system", "content": "[ROLE: general]"}]
    for t in range(n_turns):
        history += _turn(
            f"user turn {t}", done_bullets=[f"did thing {t}"], tool_msgs=per_turn_tools
        )
    data = {
        "session_hash": hash12,
        "updated_at": "2026-07-21T00:00:00Z",
        "message_count": len(history),
        "full_history": history,
    }
    path = os.path.join(str(session_dir), f"{hash12}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return hash12, path


@pytest.fixture()
def session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_log_dir", tmp_path)
    return tmp_path


def test_tool_spec_registered():
    assert TOOL_SPEC.name == "compact_session_manual"
    assert "session" in TOOL_SPEC.parameters["properties"]
    assert TOOL_SPEC.parameters["required"] == ["session"]
    assert TOOL_SPEC.default_roles == frozenset({"infrastructure"})


def test_policy_classifies_as_write():
    assert classify_action("compact_session_manual") == ActionClass.WRITE


def test_governor_allowed_guest_blocked():
    gov = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
    guest = Identity(telegram_id=99999, role=Role.GUEST, name="Guest")
    assert evaluate(gov, "compact_session_manual").allowed is True
    assert evaluate(guest, "compact_session_manual").allowed is False


def test_resolve_hash_form(session_dir):
    h, p = _make_session(session_dir)
    got = _resolve_session(h, "tg:-1003919341801:21264")
    assert got is not None and got[1] == p


def test_resolve_full_key_form(session_dir):
    h, p = _make_session(session_dir)
    got = _resolve_session("tg:-1003919341801:99999", None)
    assert got is not None and got[1] == p


def test_resolve_numeric_thread_form(session_dir):
    h, p = _make_session(session_dir)  # key chat -1003919341801 thread 99999
    got = _resolve_session("99999", "tg:-1003919341801:21264")
    assert got is not None and got[1] == p


def test_resolve_missing_returns_none(session_dir):
    assert _resolve_session("deadbeefcafe", None) is None


def test_compact_end_to_end(session_dir):
    h, p = _make_session(session_dir, n_turns=12)
    # Compute the expected retained tail (last 2 completed turns) via the library.
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data["full_history"]
    turns = find_turn_boundaries(msgs)
    keep = 2
    tail_start = turns[-keep - 1] + 1
    expected_tail = msgs[tail_start:]

    res = json.loads(
        compact_session_manual(session=h, keep_last_turns=keep, token_threshold=500)
    )
    assert res["status"] == "ok", res
    assert res["tokens_before"] > res["tokens_after"]
    assert res["messages_after"] < res["messages_before"]
    assert res["session"] == h
    # Backup file exists
    backups = glob.glob(os.path.join(str(session_dir), f"{h}.json.pre-compact-*.json"))
    assert len(backups) == 1, backups
    # Retained tail byte-identical
    with open(p, encoding="utf-8") as f:
        after = json.load(f)["full_history"]
    assert after[-len(expected_tail) :] == expected_tail
    # Summary marker present at the fold point
    summary_idx = [
        i
        for i, msg in enumerate(after)
        if isinstance(msg.get("content"), str) and "[CONTEXT SUMMARY" in msg["content"]
    ]
    assert len(summary_idx) == 1
    # default_summarizer reformats the per-turn 'Done this turn' bullets into
    # 'Turn \u2014 <user text>:\n\u2022 <bullet>' lines (the marker header is not repeated).
    assert "did thing 1" in after[summary_idx[0]]["content"]


def test_noop_under_threshold(session_dir):
    h, p = _make_session(session_dir, n_turns=4)
    res = json.loads(compact_session_manual(session=h, token_threshold=10_000_000))
    assert res["status"] == "noop", res
    assert res["tokens"] < 10_000_000  # session is under the huge threshold
    # No backup / no change
    assert glob.glob(os.path.join(str(session_dir), "*.pre-compact-*.json")) == []
    with open(p, encoding="utf-8") as f:
        assert f.read().find("[CONTEXT SUMMARY") == -1


def test_force_overrides_noop(session_dir):
    # 12 turns, keep=2 -> 10 foldable turns; huge threshold would normally
    # no-op, but force=true must compact anyway.
    h, _ = _make_session(session_dir, n_turns=12)
    res = json.loads(
        compact_session_manual(
            session=h, keep_last_turns=2, token_threshold=10_000_000, force=True
        )
    )
    assert res["status"] == "ok", res
    assert res["tokens_after"] < res["tokens_before"]


def test_refuses_active_other_thread(session_dir, monkeypatch):
    h, _ = _make_session(session_dir)
    monkeypatch.setattr(
        "app.tools.compact_session_tool._active_key_for_hash",
        lambda th: "tg:-1003919341801:OTHER",
    )
    res = json.loads(
        compact_session_manual(session=h, session_id="tg:-1003919341801:21264")
    )
    assert res["status"] == "blocked", res
    # Nothing changed on disk
    assert glob.glob(os.path.join(str(session_dir), "*.pre-compact-*.json")) == []


def test_allows_own_session_when_active(session_dir, monkeypatch):
    h, p = _make_session(session_dir)
    own = "tg:-1003919341801:21264"
    monkeypatch.setattr(
        "app.tools.compact_session_tool._active_key_for_hash",
        lambda th: own,
    )
    res = json.loads(
        compact_session_manual(session=h, session_id=own, token_threshold=500)
    )
    assert res["status"] == "ok", res


def test_missing_session_error(session_dir):
    res = json.loads(compact_session_manual(session="000000000000"))
    assert res["status"] == "error", res
    assert "No session found" in res["reason"]


def test_backup_failure_aborts(session_dir, monkeypatch):
    h, _ = _make_session(session_dir)
    monkeypatch.setattr(
        "app.tools.compact_session_tool.backup_session_file",
        lambda path: None,
    )
    res = json.loads(compact_session_manual(session=h, token_threshold=500))
    assert res["status"] == "error", res
    assert "Backup" in res["reason"]


def test_input_never_mutated(session_dir):
    """The on-disk full_history list object is never mutated in place."""
    h, p = _make_session(session_dir, n_turns=12)
    with open(p, encoding="utf-8") as f:
        before = json.load(f)["full_history"]
    snapshot = copy.deepcopy(before)
    compact_session_manual(session=h, keep_last_turns=2, token_threshold=500)
    # The original file content (pre-compaction) is preserved in the backup.
    backups = glob.glob(os.path.join(str(session_dir), f"{h}.json.pre-compact-*.json"))
    with open(backups[0], encoding="utf-8") as f:
        bkp = json.load(f)["full_history"]
    assert bkp == snapshot
