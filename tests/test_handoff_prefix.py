"""Tests for the handoff thread -> plan resolver used to make a bare 'go for it'
in a Telegram topic resolve to the right plan (SOPHIA_HANDOFFS.md registry)."""

from app.telegram_adapter import _parse_handoff_plan

REGISTRY = """\
## Registry (newest first)

| Date | Handoff | Plan file | Topic | thread_id | session_id (to rejoin) | Status |
|------|---------|-----------|-------|-----------|------------------------|--------|
| 2026-06-08 | Morning Oracle Standup | `MORNING_ORACLE_STANDUP_PLAN.md` | [topic](x) | 1722 | `tg:-1003919341801:1722` | active (Sophia parked PR1) |
| 2026-06-08 | DAO client fixes | `DAO_CLIENT_INTEGRATION_FIXES.md` | [topic](x) | 1695 | `tg:-1003919341801:1695` | active |
| 2026-06-07 | Old done thing | `OLD_PLAN.md` | [topic](x) | 1400 | `tg:-1003919341801:1400` | done |
"""


def test_resolves_by_thread_id():
    assert _parse_handoff_plan(REGISTRY, 1722) == "MORNING_ORACLE_STANDUP_PLAN.md"
    assert _parse_handoff_plan(REGISTRY, 1695) == "DAO_CLIENT_INTEGRATION_FIXES.md"


def test_unknown_thread_returns_none():
    assert _parse_handoff_plan(REGISTRY, 9999) is None


def test_inactive_handoff_is_ignored():
    # status 'done' must not resolve
    assert _parse_handoff_plan(REGISTRY, 1400) is None


def test_matches_via_session_id_suffix():
    # even if only the tg:...:<id> cell carries the id, it still resolves
    reg = "| x | y | `P.md` | t | | `tg:-100:5555` | active |"
    assert _parse_handoff_plan(reg, 5555) == "P.md"
