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
        _status(f"X:tg:-100:{i}", loop_status.STATE_LOST_GOAL, age="900")
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
    # Isolate from the real vault: once ENVOY_TELEGRAM_BOT_TOKEN is live the
    # Envoy-first lookup would otherwise hand this test a real bot token.
    _patch_vault(monkeypatch, FakeVault({}))
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


def test_deliver_without_token_is_an_error_not_a_crash(monkeypatch):
    # Empty env AND empty vault, and settings neutralised too: on a real box
    # config loads .env, so without this the "no token" case would resolve a
    # live token and attempt an actual Telegram post from the test suite.
    _patch_vault(monkeypatch, FakeVault({}))
    monkeypatch.setattr("app.config.settings.telegram_bot_api_key", "")
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


# --- Discord token resolution (Envoy-first) ----------------------------------


class FakeVault:
    """Minimal vault stand-in exposing get_value(name) with a name->value map.

    A missing key raises like the real Vault.get_value does, so the tests
    exercise the real exception path rather than a gentler one.
    """

    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}
        self.reads: list[str] = []

    def get_value(self, name: str) -> str:
        self.reads.append(name)
        if name not in self.values:
            raise ValueError(f"Credential '{name}' not found.")
        return self.values[name]


def _patch_vault(monkeypatch, vault):
    monkeypatch.setattr("app.vault.get_vault", lambda: vault)


def test_discord_token_prefers_envoy_vault_key(monkeypatch):
    """ENVOY_DISCORD_BOT_TOKEN in the vault must beat an ambient env token.

    This is the whole point of the change: loop nudges should post as
    envoy_truesight even when the process env still carries Sophia's token.
    """
    vault = FakeVault(
        {
            "ENVOY_DISCORD_BOT_TOKEN": "envoy-tok",
            "DISCORD_BOT_TOKEN": "sophia-vault-tok",
        }
    )
    _patch_vault(monkeypatch, vault)
    got = loop_alerts._discord_token(
        {"DISCORD_BOT_TOKEN": "sophia-env-tok", "DISCORD_TOKEN": "sophia-env-tok2"}
    )
    assert got == "envoy-tok"
    # It must not even need to consult the legacy key.
    assert vault.reads[0] == "ENVOY_DISCORD_BOT_TOKEN"
    assert "DISCORD_BOT_TOKEN" not in vault.reads


def test_discord_token_falls_back_to_env_when_envoy_empty(monkeypatch):
    """An empty/cleared Envoy key must degrade to the old chain, not crash."""
    _patch_vault(monkeypatch, FakeVault({"ENVOY_DISCORD_BOT_TOKEN": ""}))
    assert (
        loop_alerts._discord_token({"DISCORD_BOT_TOKEN": "sophia-env-tok"})
        == "sophia-env-tok"
    )


def test_discord_token_falls_back_when_envoy_key_missing(monkeypatch):
    """Key absent (ValueError from the vault) -> legacy chain still resolves."""
    _patch_vault(monkeypatch, FakeVault({"DISCORD_BOT_TOKEN": "sophia-vault-tok"}))
    assert (
        loop_alerts._discord_token({"DISCORD_TOKEN": "sophia-env-tok2"})
        == "sophia-env-tok2"
    )


def test_discord_token_falls_back_to_legacy_vault_key(monkeypatch):
    """No env, Envoy absent -> the legacy vault DISCORD_BOT_TOKEN is used."""
    _patch_vault(monkeypatch, FakeVault({"DISCORD_BOT_TOKEN": "sophia-vault-tok"}))
    assert loop_alerts._discord_token({}) == "sophia-vault-tok"


def test_discord_token_survives_vault_unavailable(monkeypatch):
    """A broken vault must not raise out of delivery -- return "" instead."""

    def _boom():
        raise RuntimeError("vault exploded")

    monkeypatch.setattr("app.vault.get_vault", _boom)
    assert loop_alerts._discord_token({}) == ""


# --- Telegram token resolution (Envoy-first) ---------------------------------


def test_telegram_token_prefers_envoy_vault_key(monkeypatch):
    """ENVOY_TELEGRAM_BOT_TOKEN must beat an ambient TELEGRAM_BOT_API_KEY.

    This is the whole point of the change: loop nudges should post as
    @nelanco_claude_bot even when the process env still carries Sophia's
    TELEGRAM_BOT_API_KEY.
    """
    vault = FakeVault({"ENVOY_TELEGRAM_BOT_TOKEN": "envoy-tg-tok"})
    _patch_vault(monkeypatch, vault)
    got = loop_alerts._telegram_token({"TELEGRAM_BOT_API_KEY": "sophia-env-tok"})
    assert got == "envoy-tg-tok"
    # It must not even need to consult the legacy key.
    assert vault.reads[0] == "ENVOY_TELEGRAM_BOT_TOKEN"
    assert "TELEGRAM_BOT_API_KEY" not in vault.reads


def test_telegram_token_falls_back_to_env_when_envoy_empty(monkeypatch):
    """An empty/cleared Envoy key must degrade to the old chain, not crash."""
    _patch_vault(monkeypatch, FakeVault({"ENVOY_TELEGRAM_BOT_TOKEN": ""}))
    assert (
        loop_alerts._telegram_token({"TELEGRAM_BOT_API_KEY": "sophia-env-tok"})
        == "sophia-env-tok"
    )


def test_telegram_token_falls_back_when_envoy_key_missing(monkeypatch):
    """Key absent (ValueError from the vault) -> the env chain still resolves."""
    _patch_vault(monkeypatch, FakeVault({}))
    assert (
        loop_alerts._telegram_token({"TELEGRAM_BOT_API_KEY": "sophia-env-tok"})
        == "sophia-env-tok"
    )


def test_telegram_token_falls_back_to_settings(monkeypatch):
    """No Envoy key and no env -> settings.telegram_bot_api_key, as before."""
    _patch_vault(monkeypatch, FakeVault({}))
    monkeypatch.setattr("app.config.settings.telegram_bot_api_key", "settings-tok")
    assert loop_alerts._telegram_token({}) == "settings-tok"


def test_telegram_token_survives_vault_unavailable(monkeypatch):
    """A broken vault must not raise out of delivery -- it falls through."""

    def _boom():
        raise RuntimeError("vault exploded")

    monkeypatch.setattr("app.vault.get_vault", _boom)
    monkeypatch.setattr("app.config.settings.telegram_bot_api_key", "settings-tok")
    assert loop_alerts._telegram_token({}) == "settings-tok"


def test_deliver_telegram_uses_envoy_token(monkeypatch):
    """End-to-end proof: the token in the send URL is Envoy's, not Sophia's."""
    captured = {}

    class Resp:
        status_code = 200
        text = "{}"

        def __init__(self):
            self.headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    _patch_vault(monkeypatch, FakeVault({"ENVOY_TELEGRAM_BOT_TOKEN": "envoy-tg-tok"}))
    alert = loop_alerts.Alert(
        session_id=TG, state="lost_goal", target=loop_alerts.target_for_session(TG)
    )
    out = loop_alerts.deliver(
        alert, dry_run=False, environ={"TELEGRAM_BOT_API_KEY": "sophia-env-tok"}
    )
    assert out["status"] == "sent"
    assert "/botenvoy-tg-tok/sendMessage" in captured["url"]
    assert "sophia-env-tok" not in captured["url"]


# ── awaiting_stale alerting ───────────────────────────────────────────────────
#
# The split is only useful if the new state actually reaches the governor. These
# pin both halves: it alerts by default, and the copy does not insult the reader
# by calling a blocked-on-you thread "nothing running".


def _stale_status(sid="s1"):
    return {
        "session_id": sid,
        "state": loop_status.STATE_AWAITING_STALE,
        "reason": "awaiting your decision — prompt posted 90000s ago and never refreshed",
        "goal": "ship the nudge split",
        "age_s": 90_000.0,
    }


def _target():
    return loop_alerts.Target(kind="telegram", chat_id="1", thread_id="2")


def test_awaiting_stale_alerts_by_default():
    assert loop_alerts.state_enabled(loop_status.STATE_AWAITING_STALE, {}) is True


def test_fresh_awaiting_still_never_alerts():
    """The whole point of keeping the two states apart."""
    assert loop_alerts.state_enabled(loop_status.STATE_AWAITING, {}) is False


def test_awaiting_stale_alert_can_be_disabled():
    env = {"LOOP_ALERTS_AWAITING_STALE": "false"}
    assert loop_alerts.state_enabled(loop_status.STATE_AWAITING_STALE, env) is False


def test_awaiting_stale_copy_asks_instead_of_reporting_a_fault():
    a = loop_alerts.Alert(
        session_id="s1",
        state=loop_status.STATE_AWAITING_STALE,
        target=_target(),
        reason=_stale_status()["reason"],
        goal="ship the nudge split",
        age_s=90_000.0,
    )
    text = a.text()
    assert "waiting on you" in text.lower()
    assert "nothing running" not in text.lower()
    assert "blocked on your reply" in text.lower()
    assert "Waiting for" in text


def test_lost_goal_copy_is_unchanged():
    a = loop_alerts.Alert(
        session_id="s1",
        state=loop_status.STATE_LOST_GOAL,
        target=_target(),
        reason="nothing running",
        goal="g",
        age_s=3600.0,
    )
    text = a.text()
    assert "nothing running" in text.lower()
    assert "Idle for" in text


def test_stale_awaiting_reaches_dispatch():
    """End of the chain: a stale-awaiting session must survive mapping + gating.

    Uses the module's ``TG`` fixture rather than a bare id, because
    ``target_for_session`` needs a transport prefix to route -- a bare id lands in
    ``run.unmapped`` and is silently undeliverable, which is its own bug class.
    """
    run = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_AWAITING_STALE)],
        FakeRedis(),
        environ={"LOOP_ALERTS_ENABLED": "true"},
        dry_run=False,
    )
    assert [a.state for a in run.planned] == [loop_status.STATE_AWAITING_STALE]
    assert run.unmapped == []


# --- fallback target ---------------------------------------------------------


def test_no_fallback_keeps_unmapped_behaviour():
    """Unset fallback must reproduce the old behaviour exactly."""
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={},
        dry_run=False,
    )
    assert run.planned == []
    assert len(run.unmapped) == 1
    assert run.fallback == []


def test_unmapped_session_routes_to_telegram_fallback():
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={"LOOP_ALERTS_FALLBACK_TARGET": "tg:-1003919341801:41214"},
        dry_run=False,
    )
    assert run.unmapped == []
    assert len(run.fallback) == 1
    assert NOC in run.fallback[0]
    assert len(run.planned) == 1
    alert = run.planned[0]
    assert alert.via_fallback is True
    assert alert.target.kind == "telegram"
    assert alert.target.chat_id == "-1003919341801"
    assert alert.target.thread_id == "41214"


def test_fallback_accepts_discord_channel_without_thread():
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_AWAITING_STALE)],
        FakeRedis(),
        environ={"LOOP_ALERTS_FALLBACK_TARGET": "dc:923008087315587072"},
        dry_run=False,
    )
    assert len(run.planned) == 1
    assert run.planned[0].target.kind == "discord"
    assert run.planned[0].target.thread_id is None
    assert run.planned[0].target.describe() == "discord:923008087315587072"


def test_malformed_fallback_fails_closed_to_unmapped():
    """A typo must not crash the sweep -- and must not invent a target."""
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={"LOOP_ALERTS_FALLBACK_TARGET": "not-a-real-target"},
        dry_run=False,
    )
    assert run.planned == []
    assert len(run.unmapped) == 1
    assert run.fallback == []


def test_mapped_session_ignores_fallback():
    """A session with its own channel must never be redirected."""
    run = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_AWAITING_STALE)],
        FakeRedis(),
        environ={
            "LOOP_ALERTS_ENABLED": "true",
            "LOOP_ALERTS_FALLBACK_TARGET": "dc:923008087315587072",
        },
        dry_run=False,
    )
    assert len(run.planned) == 1
    assert run.planned[0].via_fallback is False
    assert run.planned[0].target.kind == "telegram"


def test_fallback_text_names_its_origin_session():
    """In a shared channel the reader must know which session this is about."""
    run = loop_alerts.plan_alerts(
        [_status(NOC, loop_status.STATE_LOST_GOAL)],
        FakeRedis(),
        environ={"LOOP_ALERTS_FALLBACK_TARGET": "dc:923008087315587072"},
        dry_run=False,
    )
    text = run.planned[0].text()
    assert f"Session: {NOC}" in text


def test_fallback_text_omits_session_line_when_not_redirected():
    run = loop_alerts.plan_alerts(
        [_status(TG, loop_status.STATE_AWAITING_STALE)],
        FakeRedis(),
        environ={"LOOP_ALERTS_ENABLED": "true"},
        dry_run=False,
    )
    assert "Session: " not in run.planned[0].text()


# ---------------------------------------------------------------- multi-target


def test_fallback_targets_parses_one():
    got = loop_alerts.fallback_targets(
        {"LOOP_ALERTS_FALLBACK_TARGET": "tg:-1003919341801"}
    )
    assert len(got) == 1
    assert got[0].kind == "telegram" and got[0].chat_id == "-1003919341801"


def test_fallback_targets_parses_comma_separated_both_surfaces():
    """The governor asked for Telegram *and* Discord General."""
    got = loop_alerts.fallback_targets(
        {"LOOP_ALERTS_FALLBACK_TARGET": "tg:-1003919341801,dc:923008087315587075"}
    )
    assert [(t.kind, t.chat_id) for t in got] == [
        ("telegram", "-1003919341801"),
        ("discord", "923008087315587075"),
    ]


def test_fallback_targets_tolerates_spaces_and_dedupes():
    got = loop_alerts.fallback_targets(
        {"LOOP_ALERTS_FALLBACK_TARGET": " tg:-1003919341801 , tg:-1003919341801 "}
    )
    assert len(got) == 1, "same target twice must not double-post"


def test_fallback_targets_one_bad_entry_does_not_kill_the_others():
    got = loop_alerts.fallback_targets(
        {"LOOP_ALERTS_FALLBACK_TARGET": "nonsense,-1003919341801,dc:923008087315587075"}
    )
    assert [t.kind for t in got] == ["discord"], (
        "both junk entries dropped, discord survives"
    )


def test_fallback_targets_unset_is_empty():
    assert loop_alerts.fallback_targets({}) == []


def test_fallback_dedupe_key_differs_per_destination():
    """Without this, fan-out to two surfaces would dedupe the 2nd one away."""
    tg = loop_alerts.Target("telegram", "-1003919341801", None)
    dc = loop_alerts.Target("discord", "923008087315587075", None)
    a = loop_alerts.Alert(
        session_id="H:orphan", state="lost_goal", target=tg, via_fallback=True
    )
    b = loop_alerts.Alert(
        session_id="H:orphan", state="lost_goal", target=dc, via_fallback=True
    )
    assert a.dedupe_key != b.dedupe_key, (
        "two fallback destinations must not share a claim"
    )
    assert "telegram" in a.dedupe_key and "discord" in b.dedupe_key


def test_non_fallback_dedupe_key_unchanged():
    """Own-channel alerts keep the original key shape -- no ledger migration."""
    t = loop_alerts.Target("telegram", "-1003919341801", "41214")
    a = loop_alerts.Alert(session_id="H:thread", state="awaiting_stale", target=t)
    assert a.dedupe_key == "loop:alert:H:thread:awaiting_stale"


def test_plan_alerts_fans_out_to_every_fallback_target():
    st = [
        {
            "session_id": "H:orphan",
            "state": "lost_goal",
            "goal": "g",
            "awaiting_age_s": 7200,
        }
    ]
    run = loop_alerts.plan_alerts(
        st,
        dry_run=True,
        environ={
            "LOOP_ALERTS_FALLBACK_TARGET": "tg:-1003919341801,dc:923008087315587075",
            "LOOP_ALERTS_STATES": "lost_goal",
        },
    )
    assert len(run.planned) == 2, "one alert per destination"
    assert {a.target.kind for a in run.planned} == {"telegram", "discord"}
    assert all(a.via_fallback for a in run.planned)
    assert run.unmapped == []
    assert len(run.fallback) == 2


def test_plan_alerts_unset_fallback_still_reports_unmapped():
    st = [
        {
            "session_id": "H:orphan",
            "state": "lost_goal",
            "goal": "g",
            "awaiting_age_s": 7200,
        }
    ]
    run = loop_alerts.plan_alerts(
        st, dry_run=True, environ={"LOOP_ALERTS_STATES": "lost_goal"}
    )
    assert len(run.unmapped) == 1 and run.fallback == [] and run.planned == []
