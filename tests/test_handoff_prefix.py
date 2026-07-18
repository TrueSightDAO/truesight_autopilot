"""Tests for the handoff thread -> plan resolver used to make a bare 'go for it'
in a Telegram topic resolve to the right plan
(agentic_ai_context/handoffs/HANDOFF_MANIFEST.md registry — single source of
truth since the 2026-07-18 registry consolidation; it used to be
sophia/SOPHIA_HANDOFFS.md).

This fixture mirrors the REAL registry's column layout and Status vocabulary
(in progress / blocked / parked GO-ready / draft / deployed / completed /
superseded / demo · live) rather than the old "active"/"done" convention the
resolver used to require — that mismatch made the resolver dead code for every
real handoff (see HANDOFF_REGISTRY_CONSOLIDATION_PLAN.md)."""

from app.telegram_adapter import _parse_handoff_plan

REGISTRY = """\
| Plan file | Handoff title | Handoff date | Status | Telegram topic | message_thread_id | Resume tracker state | Last manifest update |
|-----------|---------------|--------------|--------|-----------------|--------------------|----------------------|----------------------|
| `MORNING_ORACLE_STANDUP_PLAN.md` | Morning Oracle Standup | 2026-06-08 | parked GO-ready | [topic](x) | 1722 | RESUME HERE = PR1 | 2026-06-08 |
| `DAO_CLIENT_INTEGRATION_FIXES.md` | DAO client fixes | 2026-06-08 | in progress | [topic](x) | 1695 | RESUME HERE = PR2 | 2026-06-08 |
| `BLOCKED_PLAN.md` | Something waiting on a secret | 2026-06-08 | blocked | [topic](x) | 1800 | RESUME HERE = PR1 | 2026-06-08 |
| `OLD_PLAN.md` | Old done thing | 2026-06-07 | completed | [topic](x) | 1400 | done | 2026-06-07 |
| `SUPERSEDED_PLAN.md` | Overtaken by another fix | 2026-06-06 | superseded — already implemented | [topic](x) | 1300 | n/a | 2026-06-06 |
"""


def test_resolves_by_thread_id():
    assert _parse_handoff_plan(REGISTRY, 1722) == "MORNING_ORACLE_STANDUP_PLAN.md"
    assert _parse_handoff_plan(REGISTRY, 1695) == "DAO_CLIENT_INTEGRATION_FIXES.md"


def test_resolves_blocked_status_too():
    # 'blocked' is not a terminal status — still resolvable so context can be
    # injected if the governor messages the thread again.
    assert _parse_handoff_plan(REGISTRY, 1800) == "BLOCKED_PLAN.md"


def test_unknown_thread_returns_none():
    assert _parse_handoff_plan(REGISTRY, 9999) is None


def test_completed_handoff_is_ignored():
    assert _parse_handoff_plan(REGISTRY, 1400) is None


def test_superseded_handoff_is_ignored():
    assert _parse_handoff_plan(REGISTRY, 1300) is None


def test_matches_via_thread_id_suffix_cell():
    # Even if only a `tg:...:<id>`-style cell carries the id (legacy shape),
    # it still resolves as long as no terminal-status marker is present.
    reg = "| `P.md` | y | d | in progress | t | `tg:-100:5555` | r | u |"
    assert _parse_handoff_plan(reg, 5555) == "P.md"
