"""Pure work-state classification for the loop daemon.

See ``agentic_ai_context/plans/LOOP_DAEMON_PLAN.md``.

The loop daemon answers one question per session (a session == one Telegram
thread or Discord channel):

    is Sophia *busy*, *stalled*, *waiting on a human*, or *doing nothing*?

This module is the **pure** half of that: it parses the state files the turn
loop already writes and classifies them. It performs no I/O, holds no locks and
imports nothing from the app -- so it is fully unit-testable and cannot become a
second source of truth. :mod:`app.loop_daemon` does the reading and publishing.

State precedence (first match wins):

1. ``busy``            -- a chat track is running and its lease is still valid.
2. ``stalled``         -- a chat track is running but its lease expired. A turn
                          that died without unregistering itself looks exactly
                          like this; before the daemon it looked *idle*.
3. ``awaiting``        -- not busy, and Sophia recently posted something flagged
                          resume-awaiting. This is *healthy* waiting.
4. ``lost_goal``       -- not busy, not awaiting, but the thread has an OPEN goal
                          and auto-advance is enabled. The loop dropped it.
5. ``idle``            -- none of the above. Healthy: nothing to do.

Only ``stalled`` and ``lost_goal`` are defects; :data:`DEFECT_STATES`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# ── states ────────────────────────────────────────────────────────────────────

STATE_BUSY = "busy"
STATE_STALLED = "stalled"
STATE_AWAITING = "awaiting"
#: A thread that was parked awaiting a governor decision and whose prompt has
#: since aged out of the freshness window. Split from STATE_AWAITING because
#: the two need opposite treatment: a fresh prompt is working as intended and
#: must not alert, while a stale one is the single most common way work is
#: silently abandoned -- the governor believes they are waiting on Sophia,
#: Sophia is waiting on the governor, and neither side is told.
STATE_AWAITING_STALE = "awaiting_stale"
STATE_LOST_GOAL = "lost_goal"
STATE_IDLE = "idle"

ALL_STATES: tuple[str, ...] = (
    STATE_BUSY,
    STATE_STALLED,
    STATE_AWAITING,
    STATE_AWAITING_STALE,
    STATE_LOST_GOAL,
    STATE_IDLE,
)

#: States worth a governor's attention.
#: ``STATE_AWAITING`` is deliberately absent: a fresh prompt is the system
#: working, not failing. ``STATE_AWAITING_STALE`` is deliberately present.
DEFECT_STATES: frozenset[str] = frozenset(
    {STATE_STALLED, STATE_LOST_GOAL, STATE_AWAITING_STALE}
)

# ── defaults ──────────────────────────────────────────────────────────────────

#: Fallback lease when a track omits ``expected_max_duration_s``.
DEFAULT_TRACK_LEASE_S = 120.0

#: An upper bound on a *trusted* lease. ``deploy_watcher`` sometimes gets a
#: sentinel timestamp; a wildly large lease would mask a real stall forever, so
#: anything above this is clamped to the default.
MAX_TRUSTED_LEASE_S = 86_400.0

#: How recently Sophia must have posted a resume-awaiting prompt for the thread
#: to count as *currently* awaiting a decision.
#:
#: The registries are, by design, a log of "I posted something a governor might
#: react to" -- NOT a block-state. An entry is only cleared when the reaction
#: path consumes it, so a thread answered by a plain text reply keeps its entry
#: until the 7-day TTL. Measured live: 880 Discord entries in 48h, 2457 in 7d.
#: Freshness-bounding is what stops every Discord channel classifying AWAITING.
# A resume-awaiting prompt that has gone unanswered this long is no longer
# "fresh": the thread is blocked on the governor, not on Sophia, so it must
# start nudging instead of staying silent.
#
# Was 24h. A thread waiting a full day for a decision was precisely the
# silence this surface exists to break. Alert dedupe is hourly per
# session+state (loop_alerts.DEFAULT_DEDUPE_S), so lowering this only moves
# the FIRST nudge earlier -- it does not raise the steady-state rate for a
# thread that stays parked.
DEFAULT_AWAITING_FRESH_S = 60 * 60.0

#: Track types that represent a live chat turn. ``deploy:``/``email:`` style
#: background tracks are deliberately excluded -- the daemon classifies
#: *conversations*, and a cron track must not make a thread look busy.
CHAT_TRACK_TYPES: frozenset[str] = frozenset(
    {"telegram_chat", "discord_chat", "chat", "telegram", "discord"}
)

_RUNNING_STATUSES: frozenset[str] = frozenset({"running", "active", "in_progress"})

# ── models ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lease:
    """A running chat turn's liveness lease (from ``active_tracks.json``)."""

    session_id: str
    label: str = ""
    status: str = ""
    started_at: float | None = None
    last_heartbeat: float | None = None
    expected_max_duration_s: float = DEFAULT_TRACK_LEASE_S

    @property
    def is_running(self) -> bool:
        return self.status in _RUNNING_STATUSES


@dataclass(frozen=True)
class SessionStatus:
    """One session's classified state. ``as_redis_hash`` is what gets published."""

    session_id: str
    state: str
    reason: str = ""
    age_s: float | None = None
    label: str = ""
    goal: str = ""

    @property
    def is_defect(self) -> bool:
        return self.state in DEFECT_STATES

    def as_redis_hash(self) -> dict[str, str]:
        """Flat ``str`` -> ``str`` mapping (Redis HSET has no nulls)."""
        out = {
            "session_id": self.session_id,
            "state": self.state,
            "reason": self.reason,
            "label": self.label,
            "goal": self.goal,
        }
        if self.age_s is not None:
            out["age_s"] = f"{self.age_s:.0f}"
        return out


# ── timestamp parsing ─────────────────────────────────────────────────────────


def parse_ts(value: Any) -> float | None:
    """Coerce a timestamp to epoch seconds, or ``None`` when unusable.

    Accepts epoch seconds (``float``/``int``), epoch milliseconds (values above
    :data:`_MS_THRESHOLD`), ISO-8601 strings (with ``Z`` or an offset, naive
    treated as UTC) and numeric strings. Rejects bools, NaN/inf, non-positive
    values and anything unparseable -- "unknown" must never masquerade as a
    valid instant, because every rule below is a *duration* comparison.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = _normalize_epoch(float(value))
        return seconds
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _normalize_epoch(float(text))
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _normalize_epoch(parsed.timestamp())
    return None


_MS_THRESHOLD = 1e11  # ~year 5138 in seconds, ~1973-03 in milliseconds


def _normalize_epoch(seconds: float) -> float | None:
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    if seconds >= _MS_THRESHOLD:
        seconds /= 1000.0
    return seconds


# ── tracks ────────────────────────────────────────────────────────────────────


def parse_tracks(raw: Any) -> list[Lease]:
    """Parse ``active_tracks.json`` (dict-with-``tracks`` or a bare list)."""
    items: Iterable[Any]
    if isinstance(raw, dict):
        candidate = raw.get("tracks")
        items = candidate if isinstance(candidate, list) else []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    leases: list[Lease] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("track_type") not in CHAT_TRACK_TYPES:
            continue
        session_id = _track_session_id(item)
        if not session_id:
            continue
        leases.append(
            Lease(
                session_id=session_id,
                label=str(item.get("label") or ""),
                status=str(item.get("status") or "").strip().lower(),
                started_at=parse_ts(item.get("started_at")),
                last_heartbeat=parse_ts(item.get("last_heartbeat")),
                expected_max_duration_s=_lease_seconds(item),
            )
        )
    return leases


def _track_session_id(item: dict[str, Any]) -> str:
    meta = item.get("metadata")
    if isinstance(meta, dict):
        nested = meta.get("session_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    raw_id = item.get("id")
    return raw_id.strip() if isinstance(raw_id, str) else ""


def _lease_seconds(item: dict[str, Any]) -> float:
    raw = item.get("expected_max_duration_s")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TRACK_LEASE_S
    if not math.isfinite(value) or value <= 0 or value > MAX_TRUSTED_LEASE_S:
        return DEFAULT_TRACK_LEASE_S
    return value


def lease_age(lease: Lease, now: float) -> float | None:
    """Seconds since the lease was last renewed, or ``None`` if unknowable."""
    reference = lease.last_heartbeat or lease.started_at
    if reference is None:
        return None
    return max(0.0, now - reference)


def lease_expired(lease: Lease, now: float) -> bool:
    """True when a *running* track has blown its lease.

    An unknowable lease (no timestamps at all) is reported as **not** expired:
    we would rather miss one stall than page a governor about a turn that may
    well be healthy. Fail-closed for alerting, not for silence.
    """
    age = lease_age(lease, now)
    if age is None:
        return False
    return age > lease.expected_max_duration_s


# ── resume-awaiting registries ───────────────────────────────────────────────


def parse_awaiting(raw: Any, *, now: float) -> dict[str, float]:
    """Map ``thread_id``/``channel_id`` -> freshest entry timestamp.

    Entries without a usable ``ts`` are skipped rather than guessed at.
    """
    if not isinstance(raw, dict):
        return {}
    freshest: dict[str, float] = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        ident = entry.get("thread_id") or entry.get("channel_id")
        if ident is None:
            continue
        key = str(ident).strip()
        if not key:
            continue
        ts = parse_ts(entry.get("ts"))
        if ts is None:
            continue
        if ts > freshest.get(key, 0.0):
            freshest[key] = ts
    return freshest


def awaiting_age(
    session_id: str,
    *,
    telegram_awaiting: dict[str, float],
    discord_awaiting: dict[str, float],
    now: float,
    fresh_s: float | None = DEFAULT_AWAITING_FRESH_S,
) -> float | None:
    """Seconds since Sophia posted a *fresh* resume-awaiting prompt, if any.

    Matching is by **suffix** on the session key (``...:tg:<chat>:<thread>`` /
    ``...:<dc namespace>:<guild>:<channel>``) so no assumption is needed about
    the public-key prefix. A Discord snowflake never collides with a (small)
    Telegram thread id, so the two namespaces cannot be confused in practice.
    """
    if not session_id:
        return None
    best: float | None = None
    for registry in (telegram_awaiting, discord_awaiting):
        for ident, ts in registry.items():
            if not session_id.endswith(f":{ident}"):
                continue
            age = max(0.0, now - ts)
            # ``fresh_s=None`` means "no cutoff": report the age whatever it is.
            # Callers use that to distinguish a prompt that is working from one
            # that aged out, without a second scan of the registries.
            if fresh_s is not None and age > fresh_s:
                continue
            if best is None or age < best:
                best = age
    return best


# ── classification ───────────────────────────────────────────────────────────


def _prompt_superseded(
    awaiting_any_age_s: float | None,
    last_activity_ts: float | None,
    now: float,
) -> bool:
    """True only with positive proof the session acted after the prompt.

    Deliberately conservative: no prompt age, or no activity timestamp, means
    "cannot prove it", which returns False and leaves classification unchanged.
    """
    if awaiting_any_age_s is None or last_activity_ts is None:
        return False
    try:
        prompt_ts = now - float(awaiting_any_age_s)
        return float(last_activity_ts) > prompt_ts
    except (TypeError, ValueError):
        return False


def classify_session(
    session_id: str,
    *,
    lease: Lease | None,
    awaiting_age_s: float | None,
    awaiting_any_age_s: float | None = None,
    open_goal_text: str | None = None,
    last_activity_ts: float | None = None,
    now: float,
    auto_advance: bool,
    label: str = "",
) -> SessionStatus:
    """Classify one session. Pure; never raises on odd input.

    ``last_activity_ts`` is when this session last produced a turn. It exists to
    kill a real failure mode: an awaiting prompt whose registry entry is never
    removed ages forever, so the loop keeps announcing a decision the governor
    already made (observed at 77h on a session that had replied hours earlier).

    The guard is positive-evidence-only. A *known* activity timestamp strictly
    newer than the prompt proves the governor acted after it, so the prompt is
    superseded. When the timestamp is missing or older we return the status we
    always did -- an unevidenced suppression would silently drop a real
    question, which is worse than the nag it replaces.
    """
    goal = (open_goal_text or "").strip()
    prompt_superseded = _prompt_superseded(awaiting_any_age_s, last_activity_ts, now)

    if lease is not None and lease.is_running:
        age = lease_age(lease, now)
        if lease_expired(lease, now):
            return SessionStatus(
                session_id=session_id,
                state=STATE_STALLED,
                reason=(
                    f"running but no heartbeat for {age:.0f}s "
                    f"(lease {lease.expected_max_duration_s:.0f}s)"
                    if age is not None
                    else "running with no usable lease timestamp"
                ),
                age_s=age,
                label=label or lease.label,
                goal=goal,
            )
        return SessionStatus(
            session_id=session_id,
            state=STATE_BUSY,
            reason=(
                f"heartbeat {age:.0f}s ago"
                if age is not None
                else "running (lease age unknown)"
            ),
            age_s=age,
            label=label or lease.label,
            goal=goal,
        )

    if awaiting_age_s is not None and not prompt_superseded:
        return SessionStatus(
            session_id=session_id,
            state=STATE_AWAITING,
            reason=f"awaiting decision — prompt posted {awaiting_age_s:.0f}s ago",
            age_s=awaiting_age_s,
            label=label or (lease.label if lease else ""),
            goal=goal,
        )

    # Parked awaiting a decision, but the prompt has aged out (or the registry
    # was never refreshed). This is blocked-on-the-governor, NOT lost work, so
    # it must be classified before the lost_goal fallthrough claims it with the
    # misleading reason "nothing running".
    if goal and awaiting_any_age_s is not None and not prompt_superseded:
        return SessionStatus(
            session_id=session_id,
            state=STATE_AWAITING_STALE,
            reason=(
                f"awaiting your decision \u2014 prompt posted {awaiting_any_age_s:.0f}s "
                "ago and never refreshed"
            ),
            age_s=awaiting_any_age_s,
            label=label or (lease.label if lease else ""),
            goal=goal,
        )

    if goal and auto_advance:
        return SessionStatus(
            session_id=session_id,
            state=STATE_LOST_GOAL,
            reason="open goal, nothing running, auto-advance enabled",
            label=label or (lease.label if lease else ""),
            goal=goal,
        )

    return SessionStatus(
        session_id=session_id,
        state=STATE_IDLE,
        reason="nothing running, nothing awaiting, no open goal",
        label=label or (lease.label if lease else ""),
        goal=goal,
    )


def summarize(statuses: Iterable[SessionStatus]) -> dict[str, Any]:
    """Counts per state plus the defect list, for the ``/loop/status`` surface."""
    counts = {state: 0 for state in ALL_STATES}
    defects: list[SessionStatus] = []
    total = 0
    for status in statuses:
        total += 1
        counts[status.state] = counts.get(status.state, 0) + 1
        if status.is_defect:
            defects.append(status)
    return {
        "total": total,
        "counts": counts,
        "defects": defects,
        "healthy": not defects,
    }
