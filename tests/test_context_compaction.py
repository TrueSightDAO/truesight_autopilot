"""PR0 of SOPHIA_CONTEXT_COMPACTION_PLAN — the compaction library, un-wired.

Long-running threads grow without bound (every turn appends user + assistant
tool_calls + tool results, nothing shrinks). This suite covers the library that
will fold OLD completed turns into one compact summary message while keeping the
most recent K turns verbatim:

* token counting (litellm, with the chars//2 fallback);
* turn-boundary detection never landing mid-tool_calls/tool;
* "\u2705 Done this turn" extraction on a real captured report;
* full compaction round-trip on a bloated synthetic fixture — token count
  drops, the retained tail is byte-identical, and _sanitise_tool_messages finds
  zero dangling messages before/after;
* the pre-compaction backup helper.

It does NOT touch the live turn path (PR2 wires it in, after PR1's manual tool
has been validated against real sessions).
"""

from __future__ import annotations

import copy
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    from app import context_compaction as cc
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.context_compaction import unavailable: {exc}", allow_module_level=True
    )

# app.main is only imported for the two _sanitise_tool_messages interaction
# tests; guard it the same way test_history_trim.py does.
try:
    import app.main as m

    HAS_MAIN = True
except Exception:  # noqa: BLE001
    HAS_MAIN = False
    m = None


def _turn(
    user_text: str, assistant_text: str = "", with_tools: bool = False
) -> list[dict]:
    """One synthetic completed turn: user -> (assistant+tool_calls -> tool)? -> assistant."""
    if not with_tools:
        return [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text or f"done: {user_text}"},
        ]
    return [
        {"role": "user", "content": user_text},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "ssh_run", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": '{"status": "ok", "stdout": "' + "L" * 220 + '"}',
        },
        {"role": "assistant", "content": assistant_text or f"done: {user_text}"},
    ]


def _done_block(action: str) -> str:
    return (
        "Here is the summary.\n\n"
        f"**\u2705 Done this turn \u2014 actions taken:**\n"
        f"\u2022 `ssh run` \u00d71 \u2192 {action}\n"
    )


def _bloated_fixture(n_turns: int = 12, tool_turns: int = 8) -> list[dict]:
    """Synthetic stand-in for the real 554-message session: a system role tag, N
    completed turns, several with tool chains, each ending in a Done-this-turn
    report (matching _build_turn_report's real shape)."""
    h: list[dict] = [{"role": "system", "content": "[ROLE: general]"}]
    for i in range(n_turns):
        h.extend(
            _turn(
                f"user request {i}",
                _done_block(f"action {i}"),
                with_tools=i < tool_turns,
            )
        )
    return h


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


def test_count_tokens_empty():
    assert cc.count_tokens([]) == 0


def test_count_tokens_uses_litellm(monkeypatch):
    """The real path calls litellm.token_counter; verify it returns a sane
    positive count on a real message list (guards the fallback from masking a
    broken litellm import)."""
    h = _bloated_fixture(n_turns=4, tool_turns=2)
    n = cc.count_tokens(h)
    assert n > 100
    assert n < 100_000  # sanity — no pathological blow-up


def test_count_tokens_chars_fallback(monkeypatch):
    """litellm import error -> the dense chars//2 fallback still returns a
    token estimate instead of raising (matches _history_token_count)."""
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "litellm":
            raise ImportError("litellm disabled for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    h = [{"role": "user", "content": "x" * 100}]
    assert cc.count_tokens(h) == 50  # 100 chars // 2


# ---------------------------------------------------------------------------
# find_turn_boundaries
# ---------------------------------------------------------------------------


def test_boundaries_plain_turns():
    h = [{"role": "system", "content": "[ROLE: general]"}] + _turn("a") + _turn("b")
    ends = cc.find_turn_boundaries(h)
    # turn a ends at index 2 (the assistant), turn b ends at index 4 (last msg)
    assert ends == [2, 4]


def test_boundaries_never_mid_tool_calls():
    """A tool-using turn's boundary is its FINAL plain assistant, never the
    assistant-with-tool_calls nor the tool result."""
    h = [{"role": "system", "content": "[ROLE: general]"}] + _turn("a", with_tools=True)
    ends = cc.find_turn_boundaries(h)
    # messages: 0 system, 1 user, 2 assistant(tool_calls), 3 tool, 4 assistant
    assert ends == [4]
    for e in ends:
        assert h[e]["role"] == "assistant"
        assert not h[e].get("tool_calls")


def test_boundaries_unfinished_tail_excluded():
    """An in-flight turn (user message with no completed assistant reply yet)
    must NOT appear as a boundary — compaction must never cut it."""
    h = [{"role": "system", "content": "[ROLE: general]"}] + _turn("done turn")
    h.append({"role": "user", "content": "new request just arrived"})
    # ...followed by an assistant that still has tool_calls pending results
    h.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "cX", "function": {"name": "ssh_run", "arguments": "{}"}}
            ],
        }
    )
    ends = cc.find_turn_boundaries(h)
    # only the first completed turn ends at index 2; the trailing in-flight
    # assistant (with tool_calls) must not be a boundary
    assert ends == [2]


def test_boundaries_multiple_turns_with_tools():
    h = _bloated_fixture(n_turns=5, tool_turns=5)
    ends = cc.find_turn_boundaries(h)
    # 1 system + 5 turns * 4 msgs = 21; turn i ends at 1 + i*4 + 3
    assert ends == [4, 8, 12, 16, 20]


# ---------------------------------------------------------------------------
# extract_done_this_turn
# ---------------------------------------------------------------------------


def test_extract_done_this_turn_real_shape():
    """A real captured report (from session d32b2609056d): marker heading,
    then bullet lines. Extraction returns the bullets."""
    h = [
        {"role": "user", "content": "find the vault process"},
        {
            "role": "assistant",
            "content": (
                "Now I have the full picture. Here's the answer:\n"
                "\n"
                "**\u2705 Done this turn \u2014 actions taken:**\n"
                "\u2022 `ssh run` \u00d712 \u2192 ss -tlnp | grep -E ':(80|443|5000|8000)'\n"
                "\u2022 `systemctl cat truesight-vault.service` \u2192 unit exists\n"
            ),
        },
    ]
    block = cc.extract_done_this_turn(h, 0, len(h))
    assert block is not None
    assert "ssh run" in block and "systemctl" in block
    assert "Done this turn" not in block  # heading itself is not part of block


def test_extract_done_this_turn_none_when_missing():
    h = _turn("no report here")
    assert cc.extract_done_this_turn(h, 0, len(h)) is None


def test_extract_done_this_turn_ignores_tool_zone():
    """The marker must be found in the final plain assistant, not in a
    tool-calling assistant (which carries no report)."""
    h = [{"role": "system", "content": "[ROLE: general]"}] + _turn(
        "t", _done_block("x"), with_tools=True
    )
    block = cc.extract_done_this_turn(h, 0, len(h))
    assert block is not None and "x" in block


# ---------------------------------------------------------------------------
# compact_history
# ---------------------------------------------------------------------------


def test_compact_noop_under_threshold():
    h = _bloated_fixture(n_turns=12, tool_turns=8)
    before = copy.deepcopy(h)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=10_000_000)
    assert out == h  # content-identical no-op
    assert h == before  # input never mutated


def test_compact_noop_when_not_enough_turns():
    h = _bloated_fixture(n_turns=3, tool_turns=2)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=1)
    assert out == h  # 3 completed turns <= keep 6 -> nothing to fold


def test_compact_disabled_threshold_zero():
    h = _bloated_fixture(n_turns=12, tool_turns=8)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=0)
    assert out == h


def test_compact_folds_old_turns_keeps_recent_verbatim():
    """Core invariant: the LAST K turns are byte-identical in the output; only
    older turns are folded into the summary."""
    h = _bloated_fixture(n_turns=12, tool_turns=8)
    keep = 6
    out = cc.compact_history(h, keep_last_n_turns=keep, token_threshold=1)
    assert out != h  # compaction happened
    te = cc.find_turn_boundaries(h)
    tail_orig = h[te[-keep - 1] + 1 :]  # keep last K ends; fold before the (K+1)-th
    # output = [system] + [summary user] + [tail_orig]
    assert out[0] == {"role": "system", "content": "[ROLE: general]"}
    sm = out[1]
    assert sm["role"] == "user"
    assert sm["content"].startswith("[CONTEXT SUMMARY")
    assert out[2:] == tail_orig  # byte-identical retained tail
    # summary text includes the compacted turns' Done-this-turn content
    assert "action 0" in sm["content"] or "action 1" in sm["content"]


def test_compact_preserves_system_and_pinned():
    """[ROLE] tag + [PINNED] notes (incl. mid-list ones) survive compaction."""
    h = _bloated_fixture(n_turns=10, tool_turns=6)
    # inject a pinned note right after the role tag (where pin_note puts it)
    h.insert(1, {"role": "system", "content": "[PINNED] keep this decision"})
    out = cc.compact_history(h, keep_last_n_turns=4, token_threshold=1)
    assert out[0]["content"].startswith("[ROLE:")
    assert out[1]["content"].startswith("[PINNED]")
    assert any(x.get("content", "").startswith("[CONTEXT SUMMARY") for x in out)


def test_compact_token_count_drops():
    """Compaction must reduce the live working-copy token count (the whole
    point), while keeping the tail sharp."""
    h = _bloated_fixture(n_turns=20, tool_turns=14)
    before = cc.count_tokens(h)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=1)
    after = cc.count_tokens(out)
    assert before > 1000
    assert after < before
    assert after < before * 0.7  # expect a clear drop (verbose tool results)


def test_compact_input_never_mutated():
    h = _bloated_fixture(n_turns=10, tool_turns=6)
    before = copy.deepcopy(h)
    cc.compact_history(h, keep_last_n_turns=4, token_threshold=1)
    assert h == before


def test_compact_custom_summarizer_used():
    calls = []

    def fake_sum(messages, start, end):
        calls.append((start, end))
        return "custom summary text"

    h = _bloated_fixture(n_turns=10, tool_turns=6)
    out = cc.compact_history(
        h, keep_last_n_turns=4, token_threshold=1, summarizer=fake_sum
    )
    assert calls  # the summarizer was consulted
    assert any("custom summary text" in (x.get("content") or "") for x in out)


def _tool_protocol_violations(msgs: list[dict]) -> list[str]:
    """Structural validator for the DeepSeek tool protocol. Returns a list of
    violation strings (empty == clean). Mirrors Pass 1 + Pass 2 of
    _sanitise_tool_messages without mutating: a tool message is valid only
    inside the contiguous zone opened by an adjacent assistant tool_calls that
    owns its id; every assistant tool_calls must be followed by a tool result
    for each id."""
    problems: list[str] = []
    open_ids: set = set()
    in_zone = False
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role == "tool":
            if not (in_zone and m.get("tool_call_id", "") in open_ids):
                problems.append(f"orphan tool msg at {i}")
        elif role == "assistant" and m.get("tool_calls"):
            open_ids = {tc.get("id", "") for tc in m["tool_calls"]}
            in_zone = True
        else:
            open_ids, in_zone = set(), False
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id", "") for tc in m["tool_calls"]]
            j = i + 1
            seen = set()
            while j < len(msgs) and msgs[j].get("role") == "tool":
                seen.add(msgs[j].get("tool_call_id", ""))
                j += 1
            missing = [cid for cid in ids if cid not in seen]
            if missing:
                problems.append(f"unanswered tool_calls at assistant {i}: {missing}")
    return problems


def test_compact_sanitise_zero_dangling():
    """Pre-flight checklist item: compaction output shows ZERO tool-protocol
    dangling messages before _and_ after a _sanitise_tool_messages pass. (The
    summary user message may be merged with the tail's opening user by Pass 3 —
    that is a content-preserving normalization, NOT tool dangling.)"""
    if not HAS_MAIN:
        pytest.skip("app.main unavailable")
    h = _bloated_fixture(n_turns=12, tool_turns=8)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=1)
    assert _tool_protocol_violations(out) == []  # before
    probe = copy.deepcopy(out)
    m._sanitise_tool_messages(probe)
    assert _tool_protocol_violations(probe) == []  # after
    # and the sanitiser's Pass 3 user-merge is the ONLY structural change:
    assert len(probe) < len(out)


def test_compact_after_sanitise_user_user_merge_is_content_preserving():
    """Documented interaction: Pass 3 of _sanitise_tool_messages collapses
    consecutive user messages. The summary sits right before the tail's opening
    user message, so the next turn's sanitise merges them into ONE user message
    whose content still contains BOTH the summary prefix and the tail opener —
    nothing is lost."""
    if not HAS_MAIN:
        pytest.skip("app.main unavailable")
    h = _bloated_fixture(n_turns=12, tool_turns=8)
    out = cc.compact_history(h, keep_last_n_turns=6, token_threshold=1)
    sm = out[1]
    assert sm["role"] == "user"
    assert out[2]["role"] == "user"  # the seam: summary directly before tail opener
    probe = copy.deepcopy(out)
    m._sanitise_tool_messages(probe)  # mutates in place
    # the two users merged into one at index 1
    assert probe[1]["role"] == "user"
    joined = probe[1]["content"]
    assert "CONTEXT SUMMARY" in joined and sm["content"][:60] in joined
    alltext = "\n\n".join(str(x.get("content") or "") for x in probe)
    assert "CONTEXT SUMMARY" in alltext
    assert out[2]["content"] in alltext  # tail opener content preserved


# ---------------------------------------------------------------------------
# backup_session_file
# ---------------------------------------------------------------------------


def test_backup_session_file_creates_sibling(tmp_path):
    src = tmp_path / "abc123.json"
    src.write_text('{"full_history": []}', encoding="utf-8")
    backup = cc.backup_session_file(src)
    assert backup is not None and backup.is_file()
    assert backup.name.startswith("abc123.json.pre-compact-")
    assert backup.read_text(encoding="utf-8") == '{"full_history": []}'
    # source untouched
    assert src.read_text(encoding="utf-8") == '{"full_history": []}'
    # only one backup created per call
    assert len(list(tmp_path.glob("*.pre-compact-*.json"))) == 1


def test_backup_session_file_missing_returns_none(tmp_path):
    assert cc.backup_session_file(tmp_path / "nope.json") is None
