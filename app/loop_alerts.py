"""U4 -- loop alerts: turn a classified defect into a nudge in the owning thread.

The reconciler (:mod:`app.loop_daemon`) is deliberately a *writer*: it measures
and stops. This module is the mouth. It reads the same ``loop:status:*`` keys the
daemon publishes and posts one short message into the Telegram topic / Discord
channel that owns the session, so an abandoned goal becomes visible *where the
work was happening* instead of only inside Redis.

Deliberate design choices (see LOOP_DAEMON_PLAN.md U4):

* **Separate failure domain.** Nothing here is imported by the daemon. A Telegram
  or Discord outage, a bad token, or a 500 from either API must never be able to
  break reconciliation -- the daemon keeps measuring even when messaging is down.
* **Only DEFECT states alert.** ``busy`` / ``idle`` / ``awaiting`` are not defects
  (``awaiting`` already means a human is on the hook). Shouting about those is how
  alerting dies.
* **``stalled`` is log-only by default.** A stalled turn usually resumes; paging on
  it is noise. ``lost_goal`` is the real payoff -- an open goal with nothing
  running is work that has silently been dropped.
* **Atomic hourly dedupe.** ``SET NX EX`` on ``loop:alert:<session>:<state>`` makes
  the claim a single Redis round-trip, so two overlapping sweeps can never both
  post. One alert per (session, state) per hour.
* **Bounded blast radius.** ``MAX_ALERTS_PER_SWEEP`` caps a first run, so enabling
  this against a backlog of abandoned threads does not firehose a group.
* **Dry-run never consumes the dedupe claim**, so previewing does not silence the
  real alert that follows.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

from . import loop_status

log = logging.getLogger("autopilot.loop_alerts")

#: Redis key prefix for status hashes (must match loop_daemon.STATUS_KEY_PREFIX).
STATUS_KEY_PREFIX = "loop:status:"
#: One alert per (session, state) per hour.
DEFAULT_DEDUPE_S = 3600
#: A first run against a backlog must not firehose a group.
MAX_ALERTS_PER_SWEEP = 10
#: Keep the nudge readable; goal text can be long.
MAX_GOAL_CHARS = 280

# A **permanently** dead target (deleted topic / unknown channel / blocked bot)
# can never succeed no matter how often it is retried. Because a failed send
# normally releases its dedupe claim so the next sweep can retry it, such a
# target would be retried every single sweep forever: a oneshot service flipping
# to `failed` every 10 minutes and masking genuine failures. Permanent failures
# therefore keep their claim for this long (they are quarantined, not retried).
QUARANTINE_S = 7 * 86400

# Permanent-failure signatures. Deliberately an explicit list rather than "any
# 4xx": a 403 is often a transient permissions blip and *should* be retried.
_PERMANENT_RE = re.compile(
    r"(thread not found|chat not found|channel not found|unknown channel|"
    r"bot was blocked|user is deactivated|peer_id_invalid|channel_invalid)",
    re.I,
)

# Redis set of quarantined dedupe keys, so dead targets are reportable rather
# than silently swallowed.
QUARANTINE_SET = "loop:alert:quarantined"


def is_permanent_failure(reason: str) -> bool:
    """True when retrying could never succeed (deleted topic, unknown channel).

    A ``discord 404`` is permanent; a ``discord 500`` or a timeout is not.
    """
    if not reason:
        return False
    if _PERMANENT_RE.search(reason):
        return True
    # Unknown channel/thread/channel-deleted, reported as a bare 404.
    return bool(re.search(r"\b(404)\b", reason))


_TG_SEND = "https://api.telegram.org"
_DC_API = "https://discord.com/api/v10"
_TIMEOUT = 20.0

#: session id shapes, e.g.
#:   MIIB...:tg:-1003919341801:30550        (telegram chat + topic)
#:   MIIB...:dc:923008087315587072:1564...  (discord channel + thread)
#:   MIIB...:envoy-some-handoff             (no channel -- cannot be nudged)
_TG_RE = re.compile(r":tg:(?P<chat>-?\d+)(?::(?P<thread>\d+))?$")
_DC_RE = re.compile(r":dc:(?P<channel>\d+)(?::(?P<thread>\d+))?$")


@dataclass(frozen=True)
class Target:
    """Where a session's nudge should land."""

    kind: str  # "telegram" | "discord"
    chat_id: str
    thread_id: str | None = None

    def describe(self) -> str:
        if self.thread_id:
            return f"{self.kind}:{self.chat_id}/{self.thread_id}"
        return f"{self.kind}:{self.chat_id}"


@dataclass
class Alert:
    """One planned nudge."""

    session_id: str
    state: str
    target: Target
    reason: str = ""
    goal: str = ""
    age_s: float | None = None
    #: True when the session had no channel and this was routed to the
    #: fallback target, so the text must name its origin.
    via_fallback: bool = False

    @property
    def dedupe_key(self) -> str:
        return f"loop:alert:{self.session_id}:{self.state}"

    def text(self) -> str:
        """The message a governor actually reads in their thread."""
        # The two defect classes need different wording. Telling a governor
        # "nothing running" about a thread that is parked awaiting *their* reply
        # is actively misleading -- it reads like a fault rather than a request.
        if self.state == loop_status.STATE_AWAITING_STALE:
            header = "\u23f3 Loop is waiting on you \u2014 decision needed"
            age_prefix = "Waiting for"
            hint = (
                "Nothing is broken; this thread is blocked on your reply. "
                "Answer here and I will pick up where I left off."
            )
        else:
            header = (
                "\U0001f501 Loop needs a nudge \u2014 goal left open, nothing running"
            )
            age_prefix = "Idle for"
            hint = (
                "Something stopped this thread mid-plan. Reply here to resume, "
                "or start a fresh instruction."
            )
        goal = (self.goal or "(no goal recorded)").strip()
        if len(goal) > MAX_GOAL_CHARS:
            goal = goal[: MAX_GOAL_CHARS - 1].rstrip() + "\u2026"
        lines = [header, ""]
        if self.via_fallback:
            # Posted into a shared channel because this session has none of its
            # own -- without the id, a reader there cannot tell what it refers
            # to.
            lines.append(f"Session: {self.session_id}")
        lines += [f"Goal: {goal}", f"State: {self.state}"]
        if self.reason:
            lines.append(f"Why: {self.reason}")
        if self.age_s is not None:
            lines.append(f"{age_prefix}: {_human_age(self.age_s)}")
        lines += ["", hint]
        return "\n".join(lines)


@dataclass
class AlertRun:
    """Outcome of one alert pass."""

    planned: list[Alert] = field(default_factory=list)
    sent: list[Alert] = field(default_factory=list)
    skipped_dedupe: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    #: Sessions with no channel that were routed to the fallback instead.
    fallback: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Eligible (not already claimed) alerts deferred by the per-sweep cap.
    capped: int = 0


def _human_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def target_for_session(session_id: str) -> Target | None:
    """Map a session id to its owning channel, or None when it has no channel.

    Sessions without a channel (handoff keys such as ``...:envoy-foo``) are real
    and still classify, but there is nowhere to post -- reporting those as
    ``unmapped`` rather than silently dropping them is the honest behaviour.
    """
    if not session_id:
        return None
    m = _TG_RE.search(session_id)
    if m:
        return Target("telegram", m.group("chat"), m.group("thread"))
    m = _DC_RE.search(session_id)
    if m:
        return Target("discord", m.group("channel"), m.group("thread"))
    return None


#: Where alerts for sessions with no channel of their own should land.
#: Shape mirrors a session suffix with no leading colon and no session hash,
#: e.g. ``tg:-1003919341801:41214`` or ``dc:923008087315587072``.
FALLBACK_ENV = "LOOP_ALERTS_FALLBACK_TARGET"

_FALLBACK_TG_RE = re.compile(r"^(?:tg|telegram):(?P<chat>-?\d+)(?::(?P<thread>\d+))?$")
_FALLBACK_DC_RE = re.compile(r"^(?:dc|discord):(?P<channel>\d+)(?::(?P<thread>\d+))?$")


def fallback_target(environ=None) -> Target | None:
    """Parse ``LOOP_ALERTS_FALLBACK_TARGET`` into a Target, or None.

    Some real sessions have no channel of their own (handoff keys such as
    ``...:envoy-foo``). Without a fallback those are only reported as
    ``unmapped`` -- honest, but the alert reaches nobody. Pointing this at a
    triage channel turns "nobody was told" into "a human was told, and was told
    which session it came from".

    Anything unparseable is treated as unset (fail-closed to the older
    ``unmapped`` behaviour) rather than raising: a typo in one env var must not
    stop the sweep from alerting on the sessions that *do* have channels.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(FALLBACK_ENV) or "").strip()
    if not raw:
        return None
    m = _FALLBACK_TG_RE.match(raw)
    if m:
        return Target("telegram", m.group("chat"), m.group("thread"))
    m = _FALLBACK_DC_RE.match(raw)
    if m:
        return Target("discord", m.group("channel"), m.group("thread"))
    log.warning("%s=%r is not a valid target; ignoring", FALLBACK_ENV, raw)
    return None


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def alerts_enabled(environ=None) -> bool:
    """Master switch. On by default: the whole point is to surface dropped work."""
    env = os.environ if environ is None else environ
    return _truthy(env.get("LOOP_ALERTS_ENABLED"), True)


def state_enabled(state: str, environ=None) -> bool:
    """Per-state switch -- ``lost_goal`` on, ``stalled`` off, by default."""
    env = os.environ if environ is None else environ
    if state == loop_status.STATE_LOST_GOAL:
        return _truthy(env.get("LOOP_ALERTS_LOST_GOAL"), True)
    if state == loop_status.STATE_STALLED:
        return _truthy(env.get("LOOP_ALERTS_STALLED"), False)
    # A thread blocked on the governor alerts by default: that is the whole
    # point of the split from STATE_AWAITING, which stays silent because a
    # fresh prompt is the system working as designed.
    if state == loop_status.STATE_AWAITING_STALE:
        return _truthy(env.get("LOOP_ALERTS_AWAITING_STALE"), True)
    # busy / awaiting / idle are not defects and never alert.
    return False


def _field(raw, key: str) -> str:
    """Read one hash field tolerantly (str or bytes, decode_responses either way)."""
    v = raw.get(key) if hasattr(raw, "get") else None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


def read_statuses(client, prefix: str = STATUS_KEY_PREFIX) -> list[dict]:
    """Read every published status hash. Never raises: returns [] on a bad client."""
    out: list[dict] = []
    try:
        keys = list(client.scan_iter(f"{prefix}*"))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not scan status keys: %s", exc)
        return []
    for key in keys:
        try:
            raw = client.hgetall(key)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("could not read %s: %s", key, exc)
            continue
        k = key.decode() if isinstance(key, bytes) else str(key)
        record = {
            f: _field(raw, f)
            for f in ("session_id", "state", "reason", "goal", "age_s", "label")
        }
        record["session_id"] = record["session_id"] or k[len(prefix) :]
        out.append(record)
    return out


def _age_of(record: dict) -> float | None:
    try:
        return float(record.get("age_s") or "")
    except (TypeError, ValueError):
        return None


def plan_alerts(
    statuses: list[dict],
    client=None,
    *,
    environ=None,
    now: float | None = None,
    dedupe_s: int = DEFAULT_DEDUPE_S,
    dry_run: bool = True,
    max_alerts: int = MAX_ALERTS_PER_SWEEP,
) -> AlertRun:
    """Decide who gets nudged. Sends nothing -- but see the claim warning below.

    Dedupe uses ``SET NX EX`` so the claim is atomic. In ``dry_run`` (the
    DEFAULT) we only *check* the key, so previewing does not swallow the real
    alert.

    .. warning::
       With ``dry_run=False`` this function **mutates the ledger**: it consumes
       one ``loop:alert:<session>:<state>`` claim per returned alert, which
       silences that alert for ``dedupe_s``. It is therefore NOT read-only.
       The default is ``dry_run=True`` precisely so that introspection
       ("show me what would be sent") cannot accidentally swallow alerts --
       which is exactly what happened once during rollout when this defaulted
       to ``False``. Pass ``dry_run=False`` only from the real dispatch path.
    """
    run = AlertRun()
    if not alerts_enabled(environ):
        log.info("loop alerts disabled (LOOP_ALERTS_ENABLED=false)")
        return run

    import time as _time

    moment = _time.time() if now is None else now

    # Deterministic order so a capped run is stable run-to-run.
    ordered = sorted(
        statuses,
        key=lambda r: (
            r.get("state") or "",
            -(_age_of(r) or 0.0),
            r.get("session_id") or "",
        ),
    )

    for record in ordered:
        sid = record.get("session_id") or ""
        state = record.get("state") or ""
        if not state_enabled(state, environ):
            continue
        target = target_for_session(sid)
        via_fallback = False
        if target is None:
            target = fallback_target(environ)
            if target is None:
                run.unmapped.append(f"{sid} [{state}]")
                continue
            via_fallback = True
            run.fallback.append(f"{sid} [{state}] -> {target.describe()}")
        alert = Alert(
            session_id=sid,
            state=state,
            target=target,
            reason=record.get("reason") or "",
            goal=record.get("goal") or "",
            age_s=_age_of(record),
            via_fallback=via_fallback,
        )
        # Skip already-claimed alerts BEFORE the cap -- in live mode too. The
        # authoritative claim remains the SET NX below; this read is what makes
        # the per-sweep budget honest. Capping a list that still contains
        # already-alerted sessions lets those stale entries eat every slot, so
        # the genuinely-new alerts sitting at the tail of the ordering get
        # capped again, sweep after sweep, and are never delivered.
        if client is not None:
            try:
                already = bool(client.exists(alert.dedupe_key))
            except Exception as exc:  # pragma: no cover - defensive
                run.errors.append(f"{sid}: dedupe check failed: {exc}")
                continue
            if already:
                run.skipped_dedupe.append(alert.dedupe_key)
                continue
        run.planned.append(alert)

    if len(run.planned) > max_alerts:
        run.capped = len(run.planned) - max_alerts
        log.warning(
            "capping alerts: %d planned, sending %d (increase MAX_ALERTS_PER_SWEEP to lift)",
            len(run.planned),
            max_alerts,
        )
        # Cap BEFORE claiming: a capped alert was never delivered, so it must not
        # burn its dedupe claim (that would silence it for dedupe_s).
        run.planned = run.planned[:max_alerts]

    # Live mode: consume a claim only for the alerts we are about to deliver.
    if client is not None and not dry_run:
        claimed: list[Alert] = []
        for alert in run.planned:
            try:
                ok = bool(
                    client.set(alert.dedupe_key, int(moment), nx=True, ex=dedupe_s)
                )
            except Exception as exc:  # pragma: no cover - defensive
                run.errors.append(f"{alert.session_id}: dedupe claim failed: {exc}")
                continue
            if ok:
                claimed.append(alert)
            else:
                run.skipped_dedupe.append(alert.dedupe_key)
        run.planned = claimed
    return run


# --- delivery ---------------------------------------------------------------


def _telegram_token(environ=None) -> str:
    """Resolve the Telegram bot token used to post loop alerts.

    Order matters and is deliberate (mirrors :func:`_discord_token`):

    1. Vault ``ENVOY_TELEGRAM_BOT_TOKEN`` -- loop nudges should speak as
       ``@nelanco_claude_bot`` (Envoy's own bot), not as Sophia's bot. This
       is checked BEFORE anything else so the loop's voice is controlled in
       exactly one place (the vault) and an ambient
       ``TELEGRAM_BOT_API_KEY`` in the process env can never silently
       re-attribute the nudges back to the autopilot.
    2. The original chain (env ``TELEGRAM_BOT_API_KEY`` -> settings),
       unchanged, so clearing/removing the Envoy key degrades gracefully
       instead of breaking Telegram nudges entirely.
    """
    env = os.environ if environ is None else environ

    # 1. Envoy-first. Any failure (vault unavailable, key absent, empty
    #    value) falls through to the legacy chain below -- never raises.
    envoy_tok = ""
    try:
        from .vault import get_vault

        envoy_tok = get_vault().get_value("ENVOY_TELEGRAM_BOT_TOKEN") or ""
    except Exception:  # noqa: BLE001  # pragma: no cover - vault/key absent
        envoy_tok = ""
    if envoy_tok:
        return envoy_tok

    # 2. Legacy chain, unchanged.
    tok = env.get("TELEGRAM_BOT_API_KEY") or ""
    if tok:
        return tok
    try:
        from .config import settings

        return getattr(settings, "telegram_bot_api_key", "") or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _discord_token(environ=None) -> str:
    """Resolve the Discord bot token used to post loop alerts.

    Order matters and is deliberate:

    1. Vault ``ENVOY_DISCORD_BOT_TOKEN`` -- loop nudges should speak as
       ``envoy_truesight``, not as Sophia. This is checked BEFORE anything
       else so the loop's voice is controlled in exactly one place (the
       vault) and an ambient ``DISCORD_BOT_TOKEN`` in the process env can
       never silently re-attribute the nudges back to the autopilot.
    2. The original chain (env -> settings -> vault ``DISCORD_BOT_TOKEN``),
       unchanged, so clearing/removing the Envoy key degrades gracefully
       instead of breaking Discord alerts entirely.
    """
    env = os.environ if environ is None else environ

    # 1. Envoy-first. Any failure (vault unavailable, key absent, empty
    #    value) falls through to the legacy chain below -- never raises.
    envoy_tok = ""
    try:
        from .vault import get_vault

        envoy_tok = get_vault().get_value("ENVOY_DISCORD_BOT_TOKEN") or ""
    except Exception:  # noqa: BLE001  # pragma: no cover - vault/key absent
        envoy_tok = ""
    if envoy_tok:
        return envoy_tok

    # 2. Legacy chain, unchanged.
    for name in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"):
        if env.get(name):
            return env[name]
    try:
        from .config import settings

        for attr in ("discord_bot_token", "discord_token"):
            tok = getattr(settings, attr, "") or ""
            if tok:
                return tok
    except Exception:  # pragma: no cover - defensive
        pass
    # Production convention (see config.py): the Discord token lives in the
    # VAULT, never in env. Without this fallback every Discord alert fails with
    # "DISCORD_BOT_TOKEN not configured" and the target is never reached.
    try:
        from .vault import get_vault

        return get_vault().get_value("DISCORD_BOT_TOKEN") or ""
    except Exception:  # pragma: no cover - vault unavailable
        return ""


def deliver(alert: Alert, *, dry_run: bool = False, environ=None) -> dict:
    """Post one alert. Returns ``{"status": "sent"|"dry_run"|"error", ...}``."""
    if dry_run:
        return {"status": "dry_run", "target": alert.target.describe()}
    import httpx

    env = os.environ if environ is None else environ
    try:
        if alert.target.kind == "telegram":
            token = _telegram_token(env)
            if not token:
                return {
                    "status": "error",
                    "reason": "TELEGRAM_BOT_API_KEY not configured",
                }
            payload = {
                "chat_id": alert.target.chat_id,
                "text": alert.text(),
                "disable_notification": False,
            }
            if alert.target.thread_id:
                payload["message_thread_id"] = int(alert.target.thread_id)
            r = httpx.post(
                f"{_TG_SEND}/bot{token}/sendMessage", json=payload, timeout=_TIMEOUT
            )
            body = (
                r.json()
                if r.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            if r.status_code != 200 or not body.get("ok", False):
                return {
                    "status": "error",
                    "reason": f"telegram {r.status_code}: {r.text[:200]}",
                }
            return {"status": "sent", "target": alert.target.describe()}

        if alert.target.kind == "discord":
            token = _discord_token(env)
            if not token:
                return {"status": "error", "reason": "DISCORD_BOT_TOKEN not configured"}
            # For Discord, a thread *is* a channel id, so post to the most
            # specific id available.
            channel = alert.target.thread_id or alert.target.chat_id
            r = httpx.post(
                f"{_DC_API}/channels/{channel}/messages",
                headers={"Authorization": f"Bot {token}"},
                json={"content": alert.text()[:1900]},
                timeout=_TIMEOUT,
            )
            if r.status_code not in (200, 201):
                return {
                    "status": "error",
                    "reason": f"discord {r.status_code}: {r.text[:200]}",
                }
            return {"status": "sent", "target": alert.target.describe()}

        return {
            "status": "error",
            "reason": f"unknown target kind {alert.target.kind!r}",
        }
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def deliver_all(
    run: AlertRun, *, dry_run: bool = False, environ=None, client=None
) -> AlertRun:
    """Deliver every planned alert, recording sent/errored.

    A failed send **releases its dedupe claim**, so the next sweep retries it
    instead of the alert being silently lost for an hour.
    """
    out = AlertRun(
        planned=list(run.planned),
        skipped_dedupe=list(run.skipped_dedupe),
        unmapped=list(run.unmapped),
        fallback=list(run.fallback),
        errors=list(run.errors),
        capped=run.capped,
    )
    quarantined: list[str] = []
    moment_ref = [time.time()]
    for alert in run.planned:
        result = deliver(alert, dry_run=dry_run, environ=environ)
        if result["status"] in ("sent", "dry_run"):
            out.sent.append(alert)
            log.info(
                "alert %s -> %s (%s)",
                alert.state,
                alert.target.describe(),
                result["status"],
            )
        else:
            out.errors.append(f"{alert.session_id}: {result.get('reason', 'unknown')}")
            log.warning(
                "alert failed for %s: %s", alert.session_id, result.get("reason")
            )
            if client is not None and not dry_run:
                reason = str(result.get("reason", ""))
                if is_permanent_failure(reason):
                    # Retrying cannot help. Re-claim for QUARANTINE_S so this
                    # target stops being retried every sweep, and record it so
                    # dead targets stay reportable rather than swallowed.
                    try:
                        client.set(
                            alert.dedupe_key, int(moment_ref[0] or 0), ex=QUARANTINE_S
                        )
                        client.sadd(QUARANTINE_SET, alert.dedupe_key)
                    except Exception as exc:  # pragma: no cover - defensive
                        # Never let bookkeeping break the sweep -- but do not hide
                        # it either: a silent failure here would re-open the very
                        # retry loop this branch exists to close.
                        log.warning(
                            "quarantine bookkeeping failed for %s: %s",
                            alert.session_id,
                            exc,
                        )
                    quarantined.append(f"{alert.target.describe()}: {reason[:120]}")
                    log.warning(
                        "alert target QUARANTINED for %dd (permanent failure, will not "
                        "retry): %s -- %s",
                        QUARANTINE_S // 86400,
                        alert.target.describe(),
                        reason[:160],
                    )
                else:
                    # Transient: release the claim so the next sweep retries.
                    try:
                        client.delete(alert.dedupe_key)
                    except Exception:  # pragma: no cover - defensive
                        pass
    if quarantined:
        log.warning("loop_alert_quarantined: n=%d", len(quarantined))
    return out
