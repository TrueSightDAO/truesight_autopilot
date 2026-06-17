"""Pure helpers for the Sophia auto-advance loop.

See ``agentic_ai_context/SOPHIA_AUTO_ADVANCE_PLAN.md``. A roadmap's *resume
tracker* (a markdown table with an ``Advance`` column) tells Sophia whether,
after finishing one unit (PR), she may immediately continue to the next unit or
must STOP at a gate. These helpers are PURE (no I/O, no network) so they are
trivially unit-testable; the brain calls :func:`next_action` at turn-end and the
adapter obeys the returned decision.

Marker semantics — the ``Advance`` cell on a unit row answers *"may Sophia
auto-START this unit?"*:

- ``auto``           -> yes; when the previous unit completes, start this one.
- ``gate: <reason>`` -> no; STOP before this unit and surface ``<reason>``.

The brain reads the plan's ``RESUME HERE`` pointer (which the executing turn
advances as each unit lands) to find *the next unit to run*, then reads that
unit's marker.

**Safety default:** anything unparseable (no tracker, no ``Advance`` column, no
``RESUME HERE`` pointer, unit not found, malformed marker) resolves to ``gate``
("stop and ask") — never ``auto``. Auto-advance must fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words in a RESUME-HERE target that mean "the plan is finished".
_DONE_RE = re.compile(r"\b(done|complete|completed|finished|all units|none left)\b", re.I)
_RESUME_RE = re.compile(r"RESUME\s+HERE\s*[:=]?\s*(.*)", re.I)
# Separators between a unit's short label and its description ("PR1 — parser").
_UNIT_SEPARATORS = ("—", "–", " - ")


@dataclass
class TrackerRow:
    """One data row of a resume tracker: a unit and its raw Advance marker."""

    unit: str
    advance: str


@dataclass
class AdvanceDecision:
    """The decision for what to do after a unit completes.

    ``decision`` is one of ``"auto"`` | ``"gate"`` | ``"done"``.
    """

    decision: str
    gate_reason: str | None = None
    next_unit: str | None = None


def _normalize(s: str) -> str:
    return (s or "").strip().strip("`").strip()


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """True for a markdown table separator like ``|---|:--:|``."""
    nonempty = [c for c in cells if c]
    return bool(nonempty) and all(set(c) <= set("-: ") for c in nonempty)


def _unit_key(label: str) -> str:
    """Reduce a unit label to its stable short key: the part before the
    em-dash / hyphen description. ``"PR1 — convention + parser"`` -> ``"pr1"``;
    ``"Unit 1 — init vault"`` -> ``"unit 1"``; ``"PR2"`` -> ``"pr2"``."""
    s = _normalize(label).lower()
    for sep in _UNIT_SEPARATORS:
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    return s.strip()


def parse_resume_tracker(plan_text: str) -> list[TrackerRow]:
    """Return the resume-tracker rows (unit + advance), in order.

    Locates the first markdown table that has BOTH a ``Unit`` and an
    ``Advance`` column and returns its data rows. ``[]`` if none is found."""
    rows: list[TrackerRow] = []
    header_seen = False
    unit_col = adv_col = -1
    for line in plan_text.splitlines():
        if not line.lstrip().startswith("|"):
            if header_seen:
                break  # table ended
            continue
        cells = _split_row(line)
        low = [c.lower() for c in cells]
        if not header_seen:
            if "unit" in low and "advance" in low:
                header_seen = True
                unit_col = low.index("unit")
                adv_col = low.index("advance")
            continue
        if _is_separator_row(cells):
            continue
        if unit_col < len(cells) and adv_col < len(cells):
            unit = _normalize(cells[unit_col])
            if unit:
                rows.append(TrackerRow(unit=unit, advance=_normalize(cells[adv_col])))
    return rows


def find_resume_here(plan_text: str) -> str | None:
    """Return the target text after the LAST ``RESUME HERE`` marker, or None.

    The last occurrence wins because plans repeat the pointer (a top-of-file
    hint plus the authoritative one in the resume tracker)."""
    target: str | None = None
    for line in plan_text.splitlines():
        m = _RESUME_RE.search(line)
        if m:
            tail = _normalize(m.group(1)).lstrip("*").strip()
            # strip trailing markdown emphasis / punctuation noise
            tail = tail.strip("*").strip()
            if tail:
                target = tail
    return target


def classify_marker(marker: str) -> AdvanceDecision:
    """Classify a single ``Advance`` cell value. Unknown -> gate (safe)."""
    raw = _normalize(marker)
    low = raw.lower()
    if low == "auto":
        return AdvanceDecision(decision="auto")
    if low.startswith("gate"):
        reason = raw.split(":", 1)[1].strip() if ":" in raw else ""
        return AdvanceDecision(
            decision="gate", gate_reason=reason or "(no reason given)"
        )
    return AdvanceDecision(
        decision="gate", gate_reason=f"unrecognized Advance marker: {marker!r}"
    )


def find_unit_row(rows: list[TrackerRow], target: str) -> int | None:
    """Index of the row whose unit key equals ``target``'s key, else None.

    Exact key match only (``"PR1"`` does NOT match ``"PR10"``)."""
    tkey = _unit_key(target)
    if not tkey:
        return None
    for i, r in enumerate(rows):
        if _unit_key(r.unit) == tkey:
            return i
    return None


def decision_for_unit(plan_text: str, unit: str) -> AdvanceDecision:
    """Decision for running ``unit`` (the next unit to do), from its marker."""
    rows = parse_resume_tracker(plan_text)
    if not rows:
        return AdvanceDecision(
            decision="gate", gate_reason="no resume tracker / Advance column found"
        )
    idx = find_unit_row(rows, unit)
    if idx is None:
        return AdvanceDecision(
            decision="gate", gate_reason=f"unit {unit!r} not found in resume tracker"
        )
    dec = classify_marker(rows[idx].advance)
    dec.next_unit = rows[idx].unit
    return dec


def next_action(plan_text: str, opened_pr: bool) -> AdvanceDecision:
    """High-level decision the brain emits at turn-end.

    Fails closed to ``gate`` unless the turn clearly completed a unit (a PR was
    opened) AND the plan's ``RESUME HERE`` points at a next unit marked ``auto``.

    Args:
        plan_text: the active roadmap's full markdown.
        opened_pr: whether THIS turn opened a PR (the unit-completed signal).
    """
    if not opened_pr:
        return AdvanceDecision(
            decision="gate",
            gate_reason="turn did not open a PR — halting auto-advance",
        )
    resume = find_resume_here(plan_text)
    if not resume:
        return AdvanceDecision(
            decision="gate", gate_reason="no RESUME HERE pointer in plan"
        )
    if _DONE_RE.search(resume):
        return AdvanceDecision(decision="done")
    return decision_for_unit(plan_text, resume)
