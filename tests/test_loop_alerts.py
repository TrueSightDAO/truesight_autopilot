"""Tests for U4 loop alerts: targeting, gating, dedupe, capping, delivery."""

from __future__ import annotations

import pytest

from app import loop_alerts, loop_status


class FakeRedis:
    """Minimal Redis surface loop_alerts uses: scan_iter / hgetall / set NX EX / exists / delete."""

    def __init__(self, hashes: dict[str, dict] | None = None):
        self.hashes = hashes or {}
        self.claims: dict[str, int] = {}
        self.deleted: list[str] = []

    def scan_iter(self, pattern: str):
        prefix = pattern.rstrip("*")
        return [k for k in self.hashes if k.startswith(prefix)]

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.claims:
            return None
        self.claims[key] = int(value)
        return True

    def exists(self, key):
        return 1 if key in self.claims else 0

    def delete(self, key):
        self.deleted.append(key)
        self.claims.pop(key, None)
        return 1


def _status(sid, state, goal="do the thing", reason="r", age="120"):
    return {
        "session_id": sid,
        "state": state,
        "goal": goal,
        "reason": reason,
        "age_s": age,
    }


TG = "MIIB:tg:-1003919341801:30550"
DC = "MIIB:dc:923008087315587072:1564981682308784229"
NOC = "MIIB:envoy-handoff-20261028"


# --- targeting ---------------------------------------------------------------


def test_target_telegram_thread():
    t = loop_alerts.target_for_session(TG)
    assert (t.kind, t.chat_id, t.thread_id) == ("telegram", "-1003919341801", "30550")


def test_target_discord_channel_and_thread():
    t = loop_alerts.target_for_session(DC)
    assert (t.kind, t.chat_id, t.thread_id) == (
        "discord",
        "923008087315587072",
        "1564981682308784229",
    )


def test_target_telegram_without_thread():
    t = loop_alerts.target_for_session("X:tg:12345")
    assert (t.kind, t.chat_id, t.thread_id) == ("telegram", "12345", None)


def test_target_none_when_no_channel():
    assert loop_alerts.target_for_session(NOC) is None
    assert loop_alerts.target_for_session("") is None


def test_target_describe_is_stable():
    assert (
        loop_alerts.target_for_session(TG).describe() == "telegram:-1003919341801/30550"
    )
    assert loop_alerts.target_for_session("X:tg:1").describe() == "telegram:1"


# --- gating ------------------------------------------------------------------


def test_master_switch_off_plans_nothing():
    run = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={"LOOP_ALERTS_ENABLED": "false"},
        dry_run=False,
    )
    assert run.planned == []


def test_lost_goal_on_by_default():
    assert loop_alerts.state_enabled(loop_status.STATE_LOST_GOAL, {}) is True


def test_stalled_log_only_by_default():
    """The whole point of the default: a resuming turn must not page anyone."""
    assert loop_alerts.state_enabled(loop_status.STATE_STALLED, {}) is False
    assert (
        loop_alerts.state_enabled(
            loop_status.STATE_STALLED, {"LOOP_ALERTS_STALLED": "true"}
        )
        is True
    )


@pytest.mark.parametrize(
    "state",
    [loop_status.STATE_BUSY, loop_status.STATE_IDLE, loop_status.STATE_AWAITING],
)
def test_non_defect_states_never_alert(state):
    assert loop_alerts.state_enabled(state, {"LOOP_ALERTS_STALLED": "true"}) is False


def test_only_lost_goal_planned_from_mixed_states():
    statuses = [
        _status(TG, loop_status.STATE_LOST_GOAL),
        _status(DC, loop_status.STATE_BUSY),
        _status(DC, loop_status.STATE_STALLED),
        _status(DC, loop_status.STATE_AWAITING),
    ]
    run = loop_alerts.plan_alerts(statuses, FakeRedis(), environ={}, dry_run=False)
    assert len(run.planned) == 1
    assert run.planned[0].state == loop_status.STATE_LOST_GOAL


# --- channel-less sessions ---------------------------------------------------


def test_session_without_channel_is_reported_not_dropped():
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={},
        dry_run=False,
    )
    assert run.planned == []
    assert len(run.unmapped) == 1
    assert NOC in run.unmapped[0]


# --- dedupe ------------------------------------------------------------------


def test_second_sweep_is_deduped():
    client = FakeRedis()
    statuses = [_status(TG, loop_status.STATE_LOST_GOAL)]
    first = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    second = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    assert len(first.planned) == 1
    assert second.planned == []
    assert len(second.skipped_dedupe) == 1


def test_dedupe_is_per_state():
    client = FakeRedis()
    env = {"LOOP_ALERTS_STALLED": "true"}
    a = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_LOST_GOAL)], client, environ=env, dry_run=False
    )
    b = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_STALLED)], client, environ=env, dry_run=False
    )
    assert len(a.planned) == 1 and len(b.planned) == 1


def test_dry_run_does_not_consume_dedupe_claim():
    """Previewing must not silence the real alert that follows."""
    client = FakeRedis()
    statuses = [_status(TG, loop_status.STATE_LOST_GOAL)]
    preview = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=True)
    assert len(preview.planned) == 1
    assert client.claims == {}
    real = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    assert len(real.planned) == 1


def test_dedupe_key_shape():
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )
    assert alert.dedupe_key == f"loop:alert:{TG}:lost_goal"


# --- capping -----------------------------------------------------------------


def test_capping_bounds_a_backlog_first_run():
    many = [_status(f"X:tg:-100:{i}", loop_status.STATE_LOST_GOAL) for i in range(25)]
    run = loop_alerts.plan_alerts(
        many, FakeRedis(), environ={}, dry_run=False, max_alerts=10
    )
    assert len(run.planned) == 10
    assert run.capped == 15


def test_already_claimed_alerts_do_not_consume_the_cap():
    """Regression (sweep #2, 2026-10-29): dedupe must be applied BEFORE the cap.

    Ten already-alerted sessions carried live claims; 9 genuinely-new alerts sat
    at the tail of the ordering. Capping 19 -> 10 picked only the stale ten,
    whose claims all failed, so planned=0 while the 9 real alerts were counted
    as "capped" again -- and would be, on every sweep, forever.
    """
    client = FakeRedis()
    stale = [f"X:tg:-100:{i}" for i in range(10)]
    fresh = [f"X:tg:-200:{i}" for i in range(9)]
    statuses = [_status(s, loop_status.STATE_LOST_GOAL, age="900") for s in stale] + [
        _status(s, loop_status.STATE_LOST_GOAL, age="600") for s in fresh
    ]
    # Sweep #1 already delivered the ten older ones, so they hold live claims.
    for sid in stale:
        client.claims[f"loop:alert:{sid}:lost_goal"] = 1

    run = loop_alerts.plan_alerts(
        statuses, client, environ={}, dry_run=False, max_alerts=10
    )
    # The stale ten are deduped WITHOUT consuming any of the ten slots...
    assert sorted(run.skipped_dedupe) == sorted(
        f"loop:alert:{sid}:lost_goal" for sid in stale
    )
    # ...so all nine genuinely-eligible targets get their alert, and nothing
    # is deferred. Before the fix this was planned=0 capped=9, every sweep.
    assert sorted(a.session_id for a in run.planned) == sorted(fresh)
    assert run.capped == 0


def test_cap_counts_only_eligible_alerts():
    """A capped backlog must drain: deferred alerts get sent once slots free up."""
    client = FakeRedis()
    statuses = [
        _status(f"X:tg:-100:{i}", loop_status.LOST_GOAL_PLACEHOLDER, age="900")
        for i in range(15)
    ]
    first = loop_alerts.plan_alerts(
        statuses, client, environ={}, dry_run=False, max_alerts=10
    )
    assert len(first.planned) == 10
    assert first.capped == 5
    sent_first = {a.session_id for a in first.planned}

    second = loop_alerts.plan_alerts(
        statuses, client, environ={}, dry_run=False, max_alerts=10
    )
    assert len(second.skipped_dedupe) == 10
    assert len(second.planned) == 5
    assert second.capped == 0
    # Exactly the five that were deferred, none re-sent.
    assert {a.session_id for a in second.planned}.isdisjoint(sent_first)


def test_cap_zero_is_respected():
    many = [_status(f"X:tg:-100:{i}", loop_status.STATE_LOST_GOAL) for i in range(3)]
    run = loop_alerts.plan_alerts(
        many, FakeRedis(), environ={}, dry_run=False, max_alerts=0
    )
    assert run.planned == []
    assert run.capped == 3


def test_ordering_is_deterministic():
    """A capped run must be stable run-to-run, so sort by state then age."""
    a = [
        _status("X:tg:-100:1", "lost_goal", age="10"),
        _status("X:tg:-100:2", "lost_goal", age="900"),
    ]
    r1 = loop_alerts.plan_alerts(list(a), FakeRedis(), environ={}, dry_run=False)
    r2 = loop_alerts.plan_alerts(
        list(reversed(a)), FakeRedis(), environ={}, dry_run=False
    )
    assert [x.session_id for x in r1.planned] == [x.session_id for x in r2.planned]
    assert r1.planned[0].session_id == "X:tg:-100:2"  # oldest idle first


# --- message body ------------------------------------------------------------


def test_text_contains_goal_and_state():
    alert = loop_alerts.Alert(
        session_id=TG,
        state="lost_goal",
        target=loop_alerts.target_for_session(TG),
        goal="Ship the thing",
        reason="open goal, nothing running",
        age_s=7200,
    )
    body = alert.text()
    assert "Ship the thing" in body
    assert "lost_goal" in body
    assert "2h" in body


def test_text_truncates_absurd_goal():
    alert = loop_alerts.Alert(
        session_id=TG,
        state="lost_goal",
        target=loop_alerts.target_for_session(TG),
        goal="x" * 5000,
    )
    assert len(alert.text()) < 900
    assert "\u2026" in alert.text()


def test_text_handles_missing_goal():
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )
    assert "no goal recorded" in alert.text()


# --- reading statuses --------------------------------------------------------


def test_read_statuses_decodes_and_fills_session_id():
    client = FakeRedis(
        hashes={
            "loop:status:MIIB:tg:-100:5": {
                "session_id": "",
                "state": "lost_goal",
                "goa": "",
            },
            "loop:status:MIIB:dc:9:8": {"session_id": "MIIB:dc:9:8", "state": "busy"},
        }
    )
    got = loop_alerts.read_statuses(client)
    assert len(got) == 2
    by_state = {r["state"]: r for r in got}
    assert by_state["lost_goal"]["session_id"] == "MIIB:tg:-100:5"


def test_read_statuses_survives_bad_client():
    class Boom:
        def scan_iter(self, pattern):
            raise RuntimeError("redis down")

    assert loop_alerts.read_statuses(Boom()) == []


# --- delivery ----------------------------------------------------------------


def test_dry_run_delivery_marks_dry_run_no_http(monkeypatch):
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )

    def _boom(*a, **k):
        raise AssertionError("must not perform HTTP in dry-run")

    monkeypatch.setattr("httpx.post", _boom)
    assert loop_alerts.deliver(alert, dry_run=True)["status"] == "dry_run"


def test_deliver_telegram_posts_to_thread(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )
    out = loop_alerts.deliver(
        alert, dry_run=False, environ={"TELEGRAM_BOT_API_KEY": "tok"}
    )
    assert out["status"] == "sent"
    assert "/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "-1003919341801"
    assert captured["json"]["message_thread_id"] == 30550


def test_deliver_without_token_is_an_error_not_a_crash():
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )
    out = loop_alerts.deliver(alert, dry_run=False, environ={})
    assert out["status"] == "error"
    assert "TELEGRAM_BOT_API_KEY" in out["reason"]


def test_failed_delivery_releases_dedupe_claim_so_it_retries(monkeypatch):
    """A send failure must not lose the alert for a whole hour."""
    client = FakeRedis()
    statuses = [_status(TG, loop_status.STATE_LOST_GOAL)]
    run = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    assert run.planned[0].dedupe_key in client.claims

    def fake_fail(alert, dry_run=False, environ=None):
        return {"status": "error", "reason": "telegram 500"}

    monkeypatch.setattr(loop_alerts, "deliver", fake_fail)
    out = loop_alerts.deliver_all(run, dry_run=False, environ={}, client=client)
    assert len(out.errors) == 1
    assert run.planned[0].dedupe_key not in client.claims
    # and the next sweep is free to try again
    again = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    assert len(again.planned) == 1


def test_deliver_all_counts_sent(monkeypatch):
    monkeypatch.setattr(
        loop_alerts,
        "deliver",
        lambda a, dry_run=False, environ=None: {"status": "sent"},
    )
    client = FakeRedis()
    run = loop_alerts.plan_alerts(
        [_status(TG, "lost_goal")], client, environ={}, dry_run=False
    )
    out = loop_alerts.deliver_all(run, dry_run=False, environ={}, client=client)
    assert len(out.sent) == 1
    assert out.errors == []


def test_plan_alerts_defaults_to_not_claiming():
    """Regression: the default MUST be read-only.

    During U4 rollout ``plan_alerts`` claimed by default, so a preview script
    written to "show me what would be sent" silently consumed all 19 pending
    alerts (TTL ~1h) and the next real sweep planned nothing. Introspection
    must never swallow alerts, so the safe mode is the default and callers
    must opt IN to claiming.
    """
    import inspect

    sig = inspect.signature(loop_alerts.plan_alerts)
    assert sig.parameters["dry_run"].default is True, (
        "plan_alerts must default to dry_run=True -- claiming by default made "
        "a read-only preview silence the real alerts"
    )


def test_default_call_consumes_no_claim():
    """A bare call (no dry_run kwarg) must leave the dedupe ledger untouched."""
    client = FakeRedis()
    statuses = [_status(TG, loop_status.STATE_LOST_GOAL)]
    default_call = loop_alerts.plan_alerts(statuses, client, environ={})
    assert len(default_call.planned) == 1
    assert client.claims == {}, "default call must not claim"
    real = loop_alerts.plan_alerts(statuses, client, environ={}, dry_run=False)
    assert len(real.planned) == 1, "the real send must still be available"
