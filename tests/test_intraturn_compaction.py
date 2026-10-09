"""Unit 1 of SOPHIA_INTRATURN_COMPACTION_PLAN — the intra-turn folding library.

Covers ``compact_open_turn`` / ``find_open_turn_start`` / ``_scan_rounds`` /
``default_round_summarizer``: folding the EARLY tool rounds of a single
still-in-progress turn while keeping the last K rounds raw, never splitting a
``tool_calls``/``tool`` pair, and replaying the real 94K/72K-token turn
fixtures captured in Unit 0.

Like the inter-turn suite, this touches the library only — no live turn path.
"""

from __future__ import annotations

import copy
import json
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

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "intraturn")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _round(r: int, result_chars: int = 400) -> list[dict]:
    """One complete tool round: assistant(tool_calls) + matching tool result."""
    cid = f"call_{r}"
    return [
        {
            "role": "assistant",
            "content": f"step {r}",
            "tool_calls": [
                {
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": "ssh_run",
                        "arguments": json.dumps({"cmd": f"step{r}"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": cid,
            "content": json.dumps({"status": "ok", "stdout": "R" * result_chars}),
        },
    ]


def _open_turn(
    n_rounds: int, result_chars: int = 400, trailing_inflight: bool = False
) -> list[dict]:
    """A system tag + user request + N complete rounds (+ optional in-flight
    assistant whose tool_calls still await a result)."""
    msgs: list[dict] = [
        {"role": "system", "content": "[ROLE: general]"},
        {"role": "user", "content": "investigate thoroughly"},
    ]
    for r in range(n_rounds):
        msgs.extend(_round(r, result_chars))
    if trailing_inflight:
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_pending",
                        "function": {"name": "ssh_run", "arguments": "{}"},
                    }
                ],
            }
        )
    return msgs


def _unanswered_ids(msgs: list[dict]) -> list[str]:
    """tool_call ids of any assistant tool_calls with no matching tool result."""
    out: list[str] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id", "") for tc in m["tool_calls"]]
            j = i + 1
            seen: set = set()
            while j < len(msgs) and msgs[j].get("role") == "tool":
                seen.add(msgs[j].get("tool_call_id", ""))
                j += 1
            out.extend(cid for cid in ids if cid not in seen)
    return sorted(out)


def _orphan_tool_ids(msgs: list[dict]) -> list[str]:
    """tool_call_id of any ``tool`` message not inside a valid open zone."""
    out: list[str] = []
    open_ids: set = set()
    in_zone = False
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            if not (in_zone and m.get("tool_call_id", "") in open_ids):
                out.append(m.get("tool_call_id", ""))
        elif role == "assistant" and m.get("tool_calls"):
            open_ids = {tc.get("id", "") for tc in m["tool_calls"]}
            in_zone = True
        else:
            open_ids, in_zone = set(), False
    return sorted(out)


def _load_fixture(name: str) -> list[dict]:
    with open(os.path.join(FIXTURE_DIR, name)) as f:
        return json.load(f)["messages"]


# ---------------------------------------------------------------------------
# find_open_turn_start
# ---------------------------------------------------------------------------


def test_find_open_turn_start_last_user():
    h = _open_turn(5, trailing_inflight=True)
    assert cc.find_open_turn_start(h) == 1


def test_find_open_turn_start_anchors_last_user():
    h = _open_turn(3)
    h += [{"role": "user", "content": "steer"}, {"role": "assistant", "content": "ok"}]
    assert cc.find_open_turn_start(h) == len(h) - 2


def test_find_open_turn_start_none_without_user():
    assert cc.find_open_turn_start([{"role": "system", "content": "x"}]) is None


# ---------------------------------------------------------------------------
# no-ops
# ---------------------------------------------------------------------------


def test_noop_when_rounds_under_keep():
    h = _open_turn(3, trailing_inflight=True)
    assert cc.compact_open_turn(h, keep_last_k_rounds=4, round_threshold=1) == h


def test_noop_when_both_thresholds_disabled():
    h = _open_turn(12, trailing_inflight=True)
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=4, round_threshold=0, token_threshold=0
    )
    assert out == h


def test_noop_under_thresholds():
    h = _open_turn(6, trailing_inflight=True)
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=2, round_threshold=8, token_threshold=10_000_000
    )
    assert out == h


def test_noop_no_user_anchor():
    h = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c", "function": {}}],
        },
        {"role": "tool", "tool_call_id": "c", "content": "x"},
    ]
    assert cc.compact_open_turn(h, round_threshold=1) == h


# ---------------------------------------------------------------------------
# folding correctness
# ---------------------------------------------------------------------------


def test_folds_early_rounds_keeps_last_k_verbatim():
    h = _open_turn(12, trailing_inflight=True)
    keep = 4
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=keep, round_threshold=8, token_threshold=10_000_000
    )
    assert out != h
    complete = cc.find_complete_rounds(h, 1, len(h))
    assert len(complete) == 12
    fold_end = complete[-keep][0]
    assert out[0] == h[0]  # role tag
    assert out[1] == h[1]  # the user request survives untouched
    sm = out[2]
    assert sm["role"] == "user"
    assert sm["content"].startswith("[TURN IN PROGRESS")
    assert "ssh_run" in sm["content"]
    assert out[3:] == h[fold_end:]  # retained tail byte-identical


def test_summary_mentions_folded_rounds():
    h = _open_turn(10, trailing_inflight=True)
    out = cc.compact_open_turn(h, keep_last_k_rounds=3, round_threshold=4)
    sm = next(
        m for m in out if str(m.get("content", "")).startswith("[TURN IN PROGRESS")
    )
    assert "Round 1:" in sm["content"]
    assert "Round 7:" in sm["content"]  # 10 rounds - 3 kept = 7 folded


def test_inflight_round_never_folded():
    h = _open_turn(12, trailing_inflight=True)
    out = cc.compact_open_turn(h, keep_last_k_rounds=4, round_threshold=8)
    assert out[-1] == h[-1]  # pending assistant(tool_calls) kept verbatim, last
    assert out[-1].get("tool_calls")


def test_incomplete_round_never_folded_across():
    h = _open_turn(6)
    h.append(
        {  # an incomplete round: assistant tool_calls with NO result
            "role": "assistant",
            "content": "interrupted",
            "tool_calls": [
                {
                    "id": "call_broken",
                    "function": {"name": "ssh_run", "arguments": "{}"},
                }
            ],
        }
    )
    for r in range(6, 12):
        h.extend(_round(r))
    keep = 2
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=keep, round_threshold=4, token_threshold=10_000_000
    )
    assert out != h
    # the incomplete round survives raw, and folding added no new dangling pair
    assert any(
        m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_broken" for m in out
    )
    assert _unanswered_ids(out) == _unanswered_ids(h)
    assert _orphan_tool_ids(out) == _orphan_tool_ids(h)


def test_fold_never_splits_tool_pair():
    h = _open_turn(12, trailing_inflight=True)
    out = cc.compact_open_turn(h, keep_last_k_rounds=4, round_threshold=8)
    assert _orphan_tool_ids(out) == []
    # every retained round-end cut lands on a round start, so no tool msg leads
    first_kept = out[3]
    assert first_kept.get("role") == "assistant" and first_kept.get("tool_calls")


def test_token_threshold_triggers_fold():
    h = _open_turn(12, result_chars=2000, trailing_inflight=True)
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=4, round_threshold=0, token_threshold=1
    )
    assert out != h


def test_custom_summarizer_used():
    calls = []

    def fake_sum(messages, start, end):
        calls.append((start, end))
        return "custom round summary"

    h = _open_turn(12, trailing_inflight=True)
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=4, round_threshold=8, summarizer=fake_sum
    )
    assert calls
    assert any("custom round summary" in (m.get("content") or "") for m in out)


def test_input_never_mutated():
    h = _open_turn(12, trailing_inflight=True)
    before = copy.deepcopy(h)
    cc.compact_open_turn(h, keep_last_k_rounds=4, round_threshold=8)
    assert h == before


def test_preserves_mid_region_system_message():
    h = _open_turn(4)
    h.insert(5, {"role": "system", "content": "[PINNED] mid-turn decision"})
    for r in range(4, 12):
        h.extend(_round(r))
    out = cc.compact_open_turn(h, keep_last_k_rounds=2, round_threshold=4)
    assert any(m.get("content") == "[PINNED] mid-turn decision" for m in out)


# ---------------------------------------------------------------------------
# real-fixture replay (Unit 0 captures)
# ---------------------------------------------------------------------------


def _trim_to_last_tool(msgs: list[dict]) -> list[dict]:
    """Truncate at the last ``tool`` result — the exact point mid-turn where the
    per-round hook fires, i.e. the turn is genuinely still open."""
    last = max(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    return msgs[: last + 1]


@pytest.mark.parametrize(
    "fname,keep,rthr",
    [("open_turn_discord.json", 4, 8), ("open_turn_discord_long.json", 4, 8)],
)
def test_real_fixture_replay(fname, keep, rthr):
    msgs = _trim_to_last_tool(_load_fixture(fname))
    h = [{"role": "system", "content": "[ROLE: general]"}] + copy.deepcopy(msgs)
    before = cc.count_tokens(h)
    out = cc.compact_open_turn(
        h, keep_last_k_rounds=keep, round_threshold=rthr, token_threshold=10_000_000
    )
    assert out != h
    assert out[1] == h[1]  # the capturing turn's user request survives
    assert any(str(m.get("content", "")).startswith("[TURN IN PROGRESS") for m in out)
    assert cc.count_tokens(out) < before  # the whole point
    # no NEW tool-protocol damage introduced by folding
    assert _unanswered_ids(out) == _unanswered_ids(h)
    assert _orphan_tool_ids(out) == _orphan_tool_ids(h)
