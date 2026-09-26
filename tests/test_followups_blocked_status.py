"""A `blocked` follow-up must parse and must NOT fire.

Regression guard: escalated-but-unanswerable follow-ups (waiting on a governor
decision) used to re-fire weekly forever because the only terminal statuses were
resolved/aborted, and resolving an *unresolved* item would be a lie. `blocked`
is the honest state: on the record, but not firing.
"""

from __future__ import annotations

from app import followups as F
from app.followups import _parse_block


def _body(status: str) -> str:
    return (
        "id: blocked-demo\n"
        "chat_id: -1\n"
        "thread_id: 42\n"
        "title: Demo\n"
        "created_at: 2026-09-14\n"
        f"status: {status}\n"
        "condition:\n  kind: elapsed_days\n  escalate_after_days: 14\n"
        "schedule:\n  check: weekly\n  on_escalate: ping_thread\n"
    )


def test_parse_block_accepts_blocked_status():
    parsed = _parse_block(_body("blocked"), 0)
    assert isinstance(parsed, dict), parsed
    assert parsed["status"] == "blocked"


def test_parse_block_still_rejects_unknown_status():
    parsed = _parse_block(_body("banana"), 0)
    assert isinstance(parsed, str)
    assert "invalid status" in parsed


def test_blocked_excluded_from_list_open(monkeypatch):
    monkeypatch.setattr(
        F,
        "parse_all",
        lambda: [
            {"id": "a", "status": "open"},
            {"id": "b", "status": "blocked"},
        ],
    )
    assert [f["id"] for f in F.list_open()] == ["a"]
