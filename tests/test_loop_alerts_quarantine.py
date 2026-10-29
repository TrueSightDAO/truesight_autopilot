"""Quarantine of permanently-dead alert targets.

A deleted Telegram topic returns ``400 message thread not found`` forever.
Because a failed send normally *releases* its dedupe claim so the next sweep
can retry, such a target was retried every 10 minutes indefinitely: the oneshot
service flipped to ``failed`` each sweep and masked genuine failures.

These tests pin the fix: permanent failures are quarantined (claim re-taken for
QUARANTINE_S, key recorded) while transient failures keep retrying.
"""

from __future__ import annotations

import app.loop_alerts as la


# --- classification ---------------------------------------------------------


def test_thread_not_found_is_permanent():
    """The exact live signature: a Telegram topic deleted on Telegram's side."""
    assert la.is_permanent_failure(
        'telegram 400: {"ok":false,"error_code":400,'
        '"description":"Bad Request: message thread not found"}'
    )


def test_chat_and_channel_not_found_are_permanent():
    assert la.is_permanent_failure("telegram 400: Bad Request: chat not found")
    assert la.is_permanent_failure("discord 404: Unknown Channel")
    assert la.is_permanent_failure("discord 404: unknown channel")


def test_blocked_bot_is_permanent():
    assert la.is_permanent_failure(
        "telegram 403: Forbidden: bot was blocked by the user"
    )
    assert la.is_permanent_failure("telegram 400: Bad Request: PEER_ID_INVALID")


def test_discord_404_is_permanent():
    assert la.is_permanent_failure('discord 404: {"message": "Unknown Channel"}')


def test_transient_errors_are_not_permanent():
    """These must keep retrying -- that is the whole point of releasing a claim."""
    for reason in [
        "telegram 500: Internal Server Error",
        'discord 500: {"message": "Internal Server Error"}',
        "discord 502: Bad Gateway",
        "telegram 429: Too Many Requests: retry after 3",
        "Timeout: timed out",
        "ConnectionError: connection reset",
        "telegram 403: not enough rights to send text messages",  # permissions blip
        "",
    ]:
        assert not la.is_permanent_failure(reason), reason


def test_token_not_configured_is_not_permanent():
    """A missing token is a config bug a human fixes; must not be quarantined."""
    assert not la.is_permanent_failure("DISCORD_BOT_TOKEN not configured")
    assert not la.is_permanent_failure("TELEGRAM_BOT_API_KEY not configured")


# --- behaviour --------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.store: dict[str, object] = {}
        self.set_calls: list[tuple] = []
        self.deleted: list[str] = []
        self.sadd_calls: list[tuple] = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1

    def exists(self, key):
        return 1 if key in self.store else 0

    def sadd(self, key, member):
        self.sadd_calls.append((key, member))
        return 1


def _alert(sid="s1"):
    target = la.Target(kind="telegram", chat_id="-100", thread_id="37982")
    return la.Alert(session_id=sid, state="lost_goal", target=target, goal="g")


def _run(monkeypatch, *, reason):
    """Deliver one alert with a forced send failure, returning (out, client)."""
    client = FakeClient()
    client.store[_alert().dedupe_key] = 1  # claim taken by plan_alerts
    run = la.AlertRun(planned=[_alert()])
    monkeypatch.setattr(
        la,
        "deliver",
        lambda a, dry_run=False, environ=None: {"status": "error", "reason": reason},
    )
    out = la.deliver_all(run, dry_run=False, environ={}, client=client)
    return out, client


def test_permanent_failure_quarantines_and_does_not_release(monkeypatch):
    out, client = _run(
        monkeypatch,
        reason='telegram 400: {"description":"Bad Request: message thread not found"}',
    )
    key = _alert().dedupe_key
    # NOT released -- no retry next sweep.
    assert key not in client.deleted
    # Re-claimed with the long quarantine TTL.
    reclaims = [c for c in client.set_calls if c[0] == key]
    assert reclaims, "expected a quarantining set()"
    assert reclaims[-1][3] == la.QUARANTINE_S
    # Recorded as dead so it is reportable.
    assert (la.QUARANTINE_SET, key) in client.sadd_calls
    assert out.errors
    assert any("message thread not found" in e for e in out.errors)


def test_transient_failure_still_releases_and_retries(monkeypatch):
    out, client = _run(monkeypatch, reason="telegram 500: Internal Server Error")
    key = _alert().dedupe_key
    assert key in client.deleted, "transient failure must release its claim"
    assert not [c for c in client.set_calls if c[3] == la.QUARANTINE_S]
    assert not client.sadd_calls
    assert out.errors


def test_quarantined_target_is_skipped_on_next_sweep(monkeypatch):
    """End-to-end: once quarantined, plan_alerts must not re-plan it."""
    client = FakeClient()
    key = _alert().dedupe_key
    client.store[key] = 1  # quarantined claim (long TTL, still present)
    statuses = [
        {
            "session_id": "s1",
            "state": "lost_goal",
            "reason": "heartbeat stale",
            "goal": "g",
        }
    ]
    monkeypatch.setattr(la, "target_for_session", lambda sid: _alert().target)
    run = la.plan_alerts(statuses, client=client, environ={}, dry_run=False)
    assert run.planned == [], "quarantined target must not be retried"
    assert key in run.skipped_dedupe


def test_quarantine_ttl_is_long():
    assert la.QUARANTINE_S >= 86400, "must be long enough to stop the retry loop"
