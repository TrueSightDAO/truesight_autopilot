"""Pure helpers for the Sophia auto-advance loop.

See ``agentic_ai_context/plans/SOPHIA_AUTO_ADVANCE_PLAN.md``. A roadmap's *resume
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

**Default = auto (revised 2026-06-23, see OPERATING_INSTRUCTIONS §5c).** A unit with
no / blank / unknown ``Advance`` marker, or a plan with no ``Advance`` column at all,
**auto-advances**. Sophia STOPS only when:

- the next unit is **irreversible / outward-facing** — an always-stop category gated by
  RULE not annotation (deploy / promote / merge-to-main / TDG-or-money issuance / UAT);
- its marker is an explicit ``gate: <reason>``;
- the turn opened **no PR** (non-convergence — handled in :func:`next_action`); or
- she **cannot locate** the next unit (no ``RESUME HERE`` pointer, or the unit is not
  found in a present tracker). Ambiguity about *where she is* still fails closed;
  ambiguity about *whether a plain unit may run* now resolves to ``auto``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words in a RESUME-HERE target that mean "the plan is finished".
_DONE_RE = re.compile(r"\b(done|complete|completed|finished|all units|none left)\b", re.I)
_RESUME_RE = re.compile(r"RESUME\s+HERE\s*[:=]?\s*(.*)", re.I)
# Irreversible / outward-facing work that ALWAYS gates (by rule, even with no/`auto`
# marker), matched against the next unit's text. A forgetful author cannot arm these
# for unattended auto-run. Explicit `gate:` markers remain the primary mechanism.
_ALWAYS_STOP_RE = re.compile(
    r"(?i)("
    r"\bdeploy|\bpromote\b|gh\s+repo\s+sync|clasp\s+(?:push|deploy)|"
    r"merge\s+to\s+(?:main|master)|\bto\s+prod\b|\bproduction\b|"
    r"issu\w*\s+tdg|tdg\s+issuance|mass[\s-]+approv\w*|\btreasury\b|\bpayout\b|"
    r"capital\s+injection|move\s+money|\bUAT\b|human\s+acceptance"
    r")"
)


def _always_stop_reason(text: str) -> str | None:
    """Return the matched always-stop keyword in ``text``, else None."""
    m = _ALWAYS_STOP_RE.search(text or "")
    return m.group(1) if m else None
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
    """Trim whitespace + surrounding markdown emphasis/code marks (``*``, `` ` ``).

    So a bold tracker cell like ``**PR1 — parser**`` or a pointer like
    ``**RESUME HERE**`` reduces to its plain text. Markdown bold/italic in unit
    labels was silently breaking unit-key matching (→ fail-closed gate)."""
    return (s or "").strip().strip("*`").strip("*` ")


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
    """Classify a single ``Advance`` cell value.

    Only an explicit ``gate:`` forces a stop here; blank / ``auto`` / unknown all
    resolve to ``auto`` (the default, revised 2026-06-23). Always-stop-by-rule is
    applied separately in :func:`decision_for_unit` from the unit's text."""
    raw = _normalize(marker)
    low = raw.lower()
    if low.startswith("gate"):
        reason = raw.split(":", 1)[1].strip() if ":" in raw else ""
        return AdvanceDecision(
            decision="gate", gate_reason=reason or "(no reason given)"
        )
    return AdvanceDecision(decision="auto")


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
    """Decision for running ``unit`` (the next unit to do).

    Default is ``auto`` (revised 2026-06-23). Forces ``gate`` when: the unit's text
    is an always-stop (irreversible/outward) category, its marker is an explicit
    ``gate:``, or — when a tracker is present — the unit cannot be located in it
    (ambiguity about *where she is* still fails closed). A plan with no tracker /
    no ``Advance`` column defaults to ``auto`` for the located unit."""
    rows = parse_resume_tracker(plan_text)
    if rows:
        idx = find_unit_row(rows, unit)
        if idx is None:
            return AdvanceDecision(
                decision="gate", gate_reason=f"unit {unit!r} not found in resume tracker"
            )
        next_unit = rows[idx].unit
        marker_dec = classify_marker(rows[idx].advance)
        unit_text = f"{rows[idx].unit} {rows[idx].advance}"
    else:
        # No tracker / no Advance column -> default auto, but still honor always-stop
        # detected from the RESUME-HERE target text.
        next_unit = unit
        marker_dec = AdvanceDecision(decision="auto")
        unit_text = unit

    # An explicit gate marker wins.
    if marker_dec.decision == "gate":
        marker_dec.next_unit = next_unit
        return marker_dec

    # Always-stop by rule: irreversible / outward-facing units gate even without a marker.
    reason = _always_stop_reason(unit_text) or _always_stop_reason(unit)
    if reason:
        return AdvanceDecision(
            decision="gate",
            gate_reason=f"always-stop: irreversible/outward unit ({reason})",
            next_unit=next_unit,
        )
    return AdvanceDecision(decision="auto", next_unit=next_unit)


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
