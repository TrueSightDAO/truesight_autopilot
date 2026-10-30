"""Loop daemon (U2) — the reconciler that publishes each session's work state.

Read-only w.r.t. the systems it observes. It loads state (tracks, resume
registries, goal log), asks :mod:`app.loop_status` to classify it, and publishes
the verdict to Redis. It never mutates tracks, registries, or goals — the
classifier owns the decision, this module owns the I/O.

Why a daemon at all: every one of those signals is observable only *in
aggregate*, from outside a turn. A running turn cannot see that its own lease
expired, and an idle thread cannot see that its goal was left open. Only a
process watching from the side can.

Fail-closed posture, deliberately asymmetric:

* Redis unavailable -> log and skip the publish; never crash the loop.
* Lease unparseable -> :func:`loop_status.lease_expired` reports *not
  expired*. A missed stall is far cheaper than paging on a healthy turn.
* Published keys carry a TTL of three intervals, so a dead daemon's last
  verdicts vanish instead of lingering as confident lies.

Usage::

    python -m app.loop_daemon --once          # single sweep, for cron/tests
    python -m app.loop_daemon                 # loop forever (systemd)
    python -m app.loop_daemon --dry-run       # classify, print, no Redis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import loop_status, thread_goal

log = logging.getLogger("loop_daemon")

#: Default sweep interval, seconds.
DEFAULT_INTERVAL = 60

#: Published status keys live 3x the sweep interval. A daemon that dies stops
#: refreshing, so its verdicts self-expire rather than lying indefinitely.
TTL_MULTIPLIER = 3

#: Daemon liveness key. Also TTL'd, for the same reason.
DAEMON_KEY = "loop:daemon:heartbeat"

#: Per-session verdict key prefix.
STATUS_PREFIX = "loop:status:"

#: Bound the published set so a pathological track file cannot flood Redis.
MAX_SESSIONS = 500


# ── data loading ────────────────────────────────────────────────────────────


def _tracks_path() -> Path:
    """Resolve the track file the same way :mod:`app.deploy_watcher` does."""
    env = os.environ.get("ACTIVE_TRACKS_PATH")
    if env:
        return Path(env)
    try:
        from .config import settings

        return settings.session_log_dir.parent / "data" / "active_tracks.json"
    except Exception:  # noqa: BLE001  # pragma: no cover - config import must never wedge
        return Path("data/active_tracks.json")


def load_tracks(path: Path | None = None) -> dict[str, loop_status.Lease]:
    """Map ``session_id`` -> :class:`loop_status.Lease`, most recent wins.

    Delegates parsing (timestamp normalisation, chat-track filtering, lease
    clamping) to :func:`loop_status.parse_tracks` so there is exactly one
    interpretation of the track file in the codebase.
    """
    target = path or _tracks_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read tracks at %s: %s", target, exc)
        return {}

    out: dict[str, loop_status.Lease] = {}
    for lease in loop_status.parse_tracks(raw):
        if not lease.session_id:
            continue
        # A session can accumulate more than one track (e.g. a stale one from a
        # previous turn); the live lease is whichever beat most recently.
        prev = out.get(lease.session_id)
        if prev is None or _activity_key(lease) > _activity_key(prev):
            out[lease.session_id] = lease
    return out


def _activity_key(lease: loop_status.Lease) -> float:
    """Most recent sign of life for a lease (0.0 if unknowable)."""
    return lease.last_heartbeat or lease.started_at or 0.0


def _registry_path(filename: str) -> Path:
    env = os.environ.get("SESSION_LOG_DIR")
    base = Path(env) if env else None
    if base is None:
        try:
            from .config import settings

            base = settings.session_log_dir
        except Exception:  # noqa: BLE001  # pragma: no cover
            base = Path("sessions")
    return base / filename


def _load_json_object(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_awaiting(now: float) -> tuple[dict[str, float], dict[str, float]]:
    """Return ``(telegram, discord)`` awaiting maps: thread/channel id -> ts."""
    tg_raw = _load_json_object(_registry_path("_resume_awaiting.json"))
    dc_raw = _load_json_object(_registry_path("_discord_resume_awaiting.json"))
    return (
        loop_status.parse_awaiting(tg_raw, now=now),
        loop_status.parse_awaiting(dc_raw, now=now),
    )


def load_goals() -> dict[str, str]:
    """Map ``session_id`` -> *open* goal text, for sessions with a live goal."""
    path_env = os.environ.get("THREAD_GOALS_PATH")
    path = Path(path_env) if path_env else None
    out: dict[str, str] = {}
    if path is not None:
        sids = _session_ids_in(path)
    else:
        sids = _session_ids_in(thread_goal._state_path())
    for sid in sids:
        try:
            goal = thread_goal.current_goal(sid, path=path)
        except Exception:  # pragma: no cover - one bad goal must not kill a sweep
            log.warning("goal replay failed for %s", sid, exc_info=True)
            continue
        if goal is not None and goal.status == thread_goal.GOAL_OPEN:
            out[sid] = goal.goal_text
    return out


def _session_ids_in(path: Path) -> list[str]:
    """Distinct session ids present in the goal log (single pass)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    seen: dict[str, None] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            sid = str(ev.get("session_id") or "").strip()
            if sid:
                seen.setdefault(sid, None)
    return list(seen)


# ── reconciliation ──────────────────────────────────────────────────────────


@dataclass
class Sweep:
    """One pass of the reconciler: what it saw and what it published."""

    statuses: list[loop_status.SessionStatus] = field(default_factory=list)
    published: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def defects(self) -> list[loop_status.SessionStatus]:
        return [s for s in self.statuses if s.is_defect]

    def by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in self.statuses:
            counts[status.state] = counts.get(status.state, 0) + 1
        return counts


def reconcile(
    *,
    now: float | None = None,
    auto_advance: bool | None = None,
    tracks_path: Path | None = None,
) -> Sweep:
    """Classify every known session once. Pure I/O -> classify, no Redis."""
    moment = time.time() if now is None else now
    if auto_advance is None:
        auto_advance = _auto_advance_enabled()

    leases = load_tracks(tracks_path)
    telegram_awaiting, discord_awaiting = load_awaiting(moment)
    goals = load_goals()

    # Sessions appear in any of the three sources; a goal opened but never
    # turned (no track yet) is exactly the `lost_goal` case worth catching.
    session_ids = list(dict.fromkeys([*leases, *goals]))
    if len(session_ids) > MAX_SESSIONS:
        log.warning(
            "truncating %d sessions to %d for publish", len(session_ids), MAX_SESSIONS
        )
        session_ids = session_ids[:MAX_SESSIONS]

    sweep = Sweep()
    for sid in session_ids:
        lease = leases.get(sid)
        try:
            status = loop_status.classify_session(
                sid,
                lease=lease,
                awaiting_age_s=loop_status.awaiting_age(
                    sid,
                    telegram_awaiting=telegram_awaiting,
                    discord_awaiting=discord_awaiting,
                    now=moment,
                ),
                # Same scan, no freshness cutoff: lets the classifier tell a
                # prompt that is still working from one that aged out.
                awaiting_any_age_s=loop_status.awaiting_age(
                    sid,
                    telegram_awaiting=telegram_awaiting,
                    discord_awaiting=discord_awaiting,
                    now=moment,
                    fresh_s=None,
                ),
                open_goal_text=goals.get(sid),
                now=moment,
                auto_advance=auto_advance,
                label=lease.label if lease is not None else "",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("classify failed for %s: %s", sid, exc, exc_info=True)
            sweep.errors.append(f"{sid}: {exc}")
            continue
        sweep.statuses.append(status)
    return sweep


def _auto_advance_enabled() -> bool:
    """Whether the goal loop is live. Default true, matching the box's config."""
    env = os.environ.get("AUTO_ADVANCE")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from .config import settings

        return bool(getattr(settings, "auto_advance", True))
    except Exception:  # noqa: BLE001  # pragma: no cover
        return True


# ── publishing ──────────────────────────────────────────────────────────────


def publish(
    sweep: Sweep,
    *,
    client: Any = None,
    interval: int = DEFAULT_INTERVAL,
    now: float | None = None,
) -> int:
    """Write verdicts to Redis. Returns the number of status keys written.

    Every write is TTL'd. If the daemon dies, its keys evaporate rather than
    persisting as confident-but-stale truth.
    """
    cli = client or _client()
    ttl = max(1, interval) * TTL_MULTIPLIER
    written = 0

    for status in sweep.statuses:
        key = f"{STATUS_PREFIX}{status.session_id}"
        try:
            cli.delete(key)
            payload = status.as_redis_hash()
            if payload:
                cli.hset(key, mapping=payload)
                cli.expire(key, ttl)
                written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("publish failed for %s: %s", status.session_id, exc)

    try:
        cli.set(DAEMON_KEY, int(time.time() if now is None else now), ex=ttl)
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        log.warning("daemon heartbeat failed: %s", exc)
    return written


def _client() -> Any:
    """Reuse the canonical Redis client from :mod:`app.redis_claim`."""
    from .redis_claim import get_client

    return get_client()


# ── CLI ─────────────────────────────────────────────────────────────────────


def _format_sweep(sweep: Sweep) -> str:
    counts = sweep.by_state()
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no sessions"
    lines = [f"loop_daemon: {summary}"]
    for status in sweep.defects:
        lines.append(
            f"  [{status.state}] {status.label or status.session_id}"
            f" — {status.reason or 'no reason given'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loop daemon (U2 reconciler)")
    parser.add_argument("--once", action="store_true", help="single sweep then exit")
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL, help="seconds between sweeps"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="classify and print; do not publish"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.once:
        sweep = reconcile()
        print(_format_sweep(sweep))
        if not args.dry_run:
            written = publish(sweep, interval=args.interval)
            print(f"published {written} status key(s)")
        return 0

    stopping = False

    def _stop(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s — finishing sweep then exiting", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info(
        "loop_daemon starting (interval=%ss, dry_run=%s)", args.interval, args.dry_run
    )
    while not stopping:
        started = time.time()
        try:
            sweep = reconcile()
            log.info(_format_sweep(sweep))
            if not args.dry_run:
                publish(sweep, interval=args.interval)
        except Exception:  # pragma: no cover - the loop must survive a bad sweep
            log.exception("sweep failed")
        elapsed = time.time() - started
        # Sleep in short slices so SIGTERM is picked up promptly.
        deadline = time.time() + max(0.0, args.interval - elapsed)
        while not stopping and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
    log.info("loop_daemon stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
