"""Regression tests for the duplicate ``RESUME HERE`` marker-drift class.

Incident #2 (2026-09-15): a plan carried two ``RESUME HERE`` markers. The
authoritative (last) one used a decorated form — ``(§4) = PR1a.`` — which the
old ``_RESUME_RE`` swallowed whole, so it key-matched no resume-tracker row and
auto-advance failed closed with an opaque "unit not found". These tests pin the
parser forms that must resolve and the drift guard that must (and must not) fire.
"""

from app.auto_advance import (
    find_resume_here,
    find_unit_row,
    next_action,
    parse_resume_tracker,
    resume_marker_drift,
)

# The two live marker conventions that must both key-match (threads 30083/30279).
TRACKER = """\
# P

| Unit | Advance |
|------|---------|
| PR1a — convention | `auto` |
| PR1b — parser | `auto` |
"""


def test_find_resume_here_parses_decorated_and_arrow_forms():
    # "(§4) = PR1a." and "→ PR1a." must both reduce to key "pr1a" (the incident).
    for line, want in (
        ("RESUME HERE (§4) = PR1a.", "PR1a"),
        ("RESUME HERE → PR1a.", "PR1a"),
        ("> **RESUME HERE:** PR1a — x.", "PR1a — x"),
    ):
        target = find_resume_here(TRACKER + "\n" + line)
        assert target == want, line
        assert find_unit_row(parse_resume_tracker(TRACKER), target) == 0, line


def test_find_resume_here_skips_prose_and_citations():
    for prose in (
        "See the tracker's RESUME HERE pointer below.",
        "markers disagree: 'a' vs 'b'",
        "the `RESUME HERE` string appears in each plan",
    ):
        assert find_resume_here(prose) is None, prose


def test_find_resume_here_skips_code_span_quoted_marker():
    assert (
        find_resume_here("Use the form `**RESUME HERE:** PR2 — the signal.` now.")
        is None
    )


def test_resume_marker_drift_none_when_authoritative_marker_resolves():
    # top hint names a DIFFERENT unit than the trailer, but the trailer resolves:
    # normal duplication -> last-wins, NOT drift.
    plan = TRACKER + "\n> **RESUME HERE → PR1a.**\n\n… trailer: RESUME HERE = PR1b.\n"
    assert resume_marker_drift(plan) is None


def test_resume_marker_drift_detects_unresolvable_authoritative_marker():
    plan = TRACKER + "\n> **RESUME HERE → PR1a.**\n\n… trailer: RESUME HERE = PR9.\n"
    assert resume_marker_drift(plan) == ("PR1a", "PR9")


def test_resume_marker_drift_none_on_done_phrasing():
    plan = (
        TRACKER
        + "\n> **RESUME HERE → PR1a.**\n\n… RESUME HERE = none — all units complete.\n"
    )
    assert resume_marker_drift(plan) is None


def test_next_action_gate_with_clear_reason_on_marker_drift():
    plan = TRACKER + "\n> **RESUME HERE → PR1a.**\n\n… trailer: RESUME HERE = PR9.\n"
    d = next_action(plan, pr_opened=True, made_progress=True)
    assert d.decision == "gate"
    assert "RESUME HERE markers disagree" in d.gate_reason
    assert "PR1a" in d.gate_reason and "PR9" in d.gate_reason


def test_next_action_auto_when_duplicate_marker_forms_agree():
    plan = (
        TRACKER + "\n> **RESUME HERE → PR1a.**\n\n… trailer: RESUME HERE (§4) = PR1a.\n"
    )
    d = next_action(plan, pr_opened=True, made_progress=True)
    assert d.decision == "auto" and d.next_unit.startswith("PR1a")
