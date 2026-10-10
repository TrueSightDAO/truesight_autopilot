"""Hermetic tests for the calendar watchdog's self-marker skip guard.

The watchdog's own "[DAILY] Sophia self-check ..." series once polluted its own
digest (it pinged "confirm prep" for the digest job). `_is_self_marker` plus the
loop-level filter remove that class of noise. No network, no credentials.
"""

from datetime import datetime, timedelta, timezone

from app import calendar_watchdog as cw


def _ev(summary, **kw):
    base = {
        "id": "ev1",
        "summary": summary,
        "start": (datetime.now(timezone.utc) + timedelta(hours=10))
        .replace(microsecond=0)
        .isoformat(),
        "attendees": [],
    }
    base.update(kw)
    return base


def test_is_self_marker_matches_daily_self_check():
    assert cw._is_self_marker(
        {"summary": "[DAILY] Sophia self-check - calendar + invite follow-up"}
    )


def test_is_self_marker_matches_space_and_case_variants():
    assert cw._is_self_marker({"summary": "Sophia self check"})
    assert cw._is_self_marker({"summary": "SOPHIA SELF-CHECK"})


def test_is_self_marker_false_for_real_events():
    assert not cw._is_self_marker({"summary": "[FOLLOW-UP] Matheus - ship cacao tea"})
    assert not cw._is_self_marker({"summary": "Power of Attorney execution"})
    assert not cw._is_self_marker({"summary": ""})
    assert not cw._is_self_marker({})


def test_self_marker_would_pollute_digest_but_is_filtered():
    # Near-term, so un-filtered the marker WOULD earn an attention line...
    marker = _ev("[DAILY] Sophia self-check - calendar + invite follow-up")
    assert cw._needs_attention(marker, datetime.now(timezone.utc))

    # ...but the loop-level filter drops it before any view renders.
    events = [marker, _ev("[FOLLOW-UP] real thing")]
    kept = [e for e in events if not cw._is_self_marker(e)]
    out = cw.build_digest(kept)
    assert "Sophia self-check" not in out
    assert "[FOLLOW-UP] real thing" in out
