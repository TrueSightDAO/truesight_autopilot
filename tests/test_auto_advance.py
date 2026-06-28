"""Unit tests for app/auto_advance.py — the pure resume-tracker parser that
drives Sophia's auto-advance loop. Default is ``auto`` (revised 2026-06-23);
a ``gate`` is forced only by an always-stop category (irreversible/outward),
an explicit ``gate:`` marker, or inability to LOCATE the next unit."""

from __future__ import annotations

from app.auto_advance import (
    AdvanceDecision,
    classify_marker,
    decision_for_unit,
    find_resume_here,
    find_unit_row,
    next_action,
    parse_resume_tracker,
)

PLAN = """\
# Some Plan

> **RESUME HERE:** PR1 — do the first thing.

## 10. Resume tracker

| Unit | Advance | PR opened | Merged |
|------|---------|-----------|--------|
| PR1 — convention + parser | `auto` | ☐ | ☐ |
| PR2 — brain advance signal | `auto` | ☐ | ☐ |
| PR3 — adapter self-advance loop | `gate: deploy + UAT before go-live` | ☐ | ☐ |
| PR4 — rollout + UAT | `gate: UAT` | ☐ | ☐ |

> **RESUME HERE:** PR2 — the brain signal.
"""


# ── parse_resume_tracker ────────────────────────────────────────────────────


def test_parse_tracker_extracts_units_in_order():
    rows = parse_resume_tracker(PLAN)
    assert [r.unit.split(" —")[0] for r in rows] == ["PR1", "PR2", "PR3", "PR4"]
    assert rows[0].advance == "auto"
    assert rows[2].advance.startswith("gate:")


def test_parse_tracker_missing_advance_column_returns_empty():
    no_adv = "| Unit | PR opened |\n|------|------|\n| PR1 | ☐ |\n"
    assert parse_resume_tracker(no_adv) == []


def test_parse_tracker_no_table_returns_empty():
    assert parse_resume_tracker("just prose, no table") == []


# ── find_resume_here ────────────────────────────────────────────────────────


def test_find_resume_here_takes_last_occurrence():
    # PLAN has two RESUME HERE lines; the last (tracker) one wins.
    assert find_resume_here(PLAN).startswith("PR2")


def test_find_resume_here_none_when_absent():
    assert find_resume_here("no pointer here") is None


def test_find_resume_here_detects_done_phrasing():
    txt = "> **RESUME HERE:** none — all units complete."
    assert find_resume_here(txt).lower().startswith("none")


# ── classify_marker ─────────────────────────────────────────────────────────


def test_classify_auto():
    assert classify_marker("auto").decision == "auto"
    assert classify_marker("`auto`").decision == "auto"


def test_classify_gate_with_reason():
    d = classify_marker("gate: prod deploy — eyeball first")
    assert d.decision == "gate"
    assert "prod deploy" in d.gate_reason


def test_classify_bare_gate():
    d = classify_marker("gate")
    assert d.decision == "gate" and d.gate_reason


def test_classify_unknown_is_auto():
    # Revised 2026-06-23: blank/unknown markers default to auto (only `gate:` stops here).
    assert classify_marker("maybe?").decision == "auto"
    assert classify_marker("").decision == "auto"


# ── find_unit_row ───────────────────────────────────────────────────────────


def test_find_unit_row_matches_short_key():
    rows = parse_resume_tracker(PLAN)
    assert find_unit_row(rows, "PR3 — whatever description") == 2


def test_find_unit_row_pr1_does_not_match_pr10():
    rows = [
        type("R", (), {"unit": "PR10 — big one", "advance": "auto"})(),
    ]
    # _unit_key("PR1") != _unit_key("PR10"); no false prefix match.
    from app.auto_advance import TrackerRow

    rows = [TrackerRow(unit="PR10 — big", advance="auto")]
    assert find_unit_row(rows, "PR1") is None


# ── decision_for_unit ───────────────────────────────────────────────────────


def test_decision_for_unit_auto():
    d = decision_for_unit(PLAN, "PR2")
    assert d.decision == "auto" and d.next_unit.startswith("PR2")


def test_decision_for_unit_handles_bold_markdown_unit():
    # Bold/`code` in the unit cell must still match (was silently fail-closing).
    plan = (
        "> **RESUME HERE:** PR1 — parser\n\n"
        "| Unit | Advance | PR opened |\n|------|---------|-----------|\n"
        "| **PR1 — `create_proposal.py`** | `auto` | ☐ |\n"
    )
    d = decision_for_unit(plan, "PR1")
    assert d.decision == "auto"


def test_decision_for_unit_gate():
    d = decision_for_unit(PLAN, "PR3")
    assert d.decision == "gate" and "deploy" in d.gate_reason


def test_decision_for_unit_unknown_unit_is_gate():
    d = decision_for_unit(PLAN, "PR99")
    assert d.decision == "gate" and "not found" in d.gate_reason


def test_decision_for_unit_no_tracker_defaults_auto():
    # Revised 2026-06-23: a plan with no tracker defaults to auto for the located unit
    # (as long as the unit text isn't an always-stop category).
    d = decision_for_unit("no table at all", "PR1 — add a parser")
    assert d.decision == "auto"


def test_decision_for_unit_no_tracker_always_stop_gates():
    # ...unless the unit text is irreversible/outward — always-stop by rule, no marker needed.
    d = decision_for_unit("no table at all", "PR5 — deploy to prod")
    assert d.decision == "gate" and "always-stop" in d.gate_reason


def test_decision_for_unit_always_stop_with_auto_marker_gates():
    # A unit explicitly marked `auto` but whose text is an always-stop still gates.
    plan = (
        "> **RESUME HERE:** PR3 — promote to prod\n\n"
        "| Unit | Advance | PR opened |\n|------|---------|-----------|\n"
        "| PR3 — promote to prod | `auto` | ☐ |\n"
    )
    d = decision_for_unit(plan, "PR3")
    assert d.decision == "gate" and "always-stop" in d.gate_reason


def test_decision_for_unit_tdg_issuance_gates():
    plan = (
        "| Unit | Advance | PR opened |\n|------|---------|-----------|\n"
        "| Phase 3 — issue TDG to contributors | `auto` | ☐ |\n"
    )
    d = decision_for_unit(plan, "Phase 3")
    assert d.decision == "gate"


# ── next_action (the high-level brain call) ─────────────────────────────────


def test_next_action_auto_when_pr_opened_and_next_is_auto():
    # RESUME HERE -> PR2 (auto)
    d = next_action(PLAN, opened_pr=True)
    assert d.decision == "auto" and d.next_unit.startswith("PR2")


def test_next_action_gate_when_no_pr_opened():
    d = next_action(PLAN, opened_pr=False)
    assert d.decision == "gate" and "did not open a PR" in d.gate_reason


def test_next_action_gate_when_next_unit_gated():
    plan = PLAN.replace("**RESUME HERE:** PR2", "**RESUME HERE:** PR3")
    d = next_action(plan, opened_pr=True)
    assert d.decision == "gate" and "deploy" in d.gate_reason


def test_next_action_done_when_resume_says_complete():
    plan = PLAN + "\n> **RESUME HERE:** none — all units complete.\n"
    d = next_action(plan, opened_pr=True)
    assert d.decision == "done"


def test_next_action_gate_when_no_resume_pointer():
    # tracker present, but no RESUME HERE anywhere
    plan = "\n".join(
        ln for ln in PLAN.splitlines() if "RESUME HERE" not in ln
    )
    d = next_action(plan, opened_pr=True)
    assert d.decision == "gate" and "RESUME HERE" in d.gate_reason


def test_advance_decision_dataclass_defaults():
    d = AdvanceDecision(decision="auto")
    assert d.gate_reason is None and d.next_unit is None
