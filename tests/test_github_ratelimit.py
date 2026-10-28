"""Tests for proactive GitHub rate-limit awareness (rate-limit plan PR2).

PR1 (#553) bounded *concurrency*. It cannot bound *budget*: N concurrent calls
still spend N of the hourly allowance. These tests pin the half PR1 cannot
cover -- read the budget before firing, warn when low, wait when critical, and
never let a *transient* check failure break a GitHub operation -- but genuine
exhaustion must fail fast rather than sleep-then-403.
"""

from __future__ import annotations

import httpx
import pytest

from app import github_ratelimit as rl


@pytest.fixture(autouse=True)
def _clean_state():
    rl.reset_state()
    yield
    rl.reset_state()


def _payload(remaining: int, reset: float) -> dict:
    return {"resources": {"core": {"remaining": remaining, "reset": reset}}}


def test_parse_extracts_core_bucket():
    assert rl._parse(_payload(4321, 1_800_000_000.0)) == (4321, 1_800_000_000.0)


def test_parse_tolerates_missing_core():
    assert rl._parse({}) == (None, None)
    assert rl._parse({"resources": {}}) == (None, None)


def test_refresh_caches_within_ttl(monkeypatch):
    calls = []

    class _Resp:
        def json(self):
            return _payload(4000, 1_800_000_000.0)

    def _get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _get)
    t = [1000.0]

    def now():
        return t[0]

    assert rl.refresh("tok", now_fn=now) == (4000, 1_800_000_000.0)
    assert rl.refresh("tok", now_fn=now) == (4000, 1_800_000_000.0)
    assert len(calls) == 1, "second call inside TTL must not re-probe"

    t[0] += rl.STATE_TTL + 1
    rl.refresh("tok", now_fn=now)
    assert len(calls) == 2, "call after TTL should re-probe"


def test_refresh_never_raises_on_probe_failure(monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "get", _boom)
    assert rl.refresh("tok", now_fn=lambda: 0.0) == (None, None)


def test_wait_no_sleep_when_budget_healthy(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type(
            "R", (), {"json": lambda self: _payload(4500, 9_999_999_999.0)}
        )(),
    )
    slept = []
    r = rl.wait_if_exhausted("tok", now_fn=lambda: 0.0, sleep_fn=slept.append)
    assert r == 0.0
    assert slept == []


def test_wait_sleeps_when_budget_critical(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type("R", (), {"json": lambda self: _payload(10, now + 5)})(),
    )
    slept = []
    r = rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=slept.append)
    assert r > 0
    assert len(slept) == 1
    assert slept[0] <= rl.MAX_SLEEP


def test_wait_caps_sleep_at_max(monkeypatch):
    now = 1_800_000_000.0
    # Budget still present but critical, reset an hour away -> capped, never an hour.
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type("R", (), {"json": lambda self: _payload(5, now + 3600)})(),
    )
    slept = []
    r = rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=slept.append)
    assert r == rl.MAX_SLEEP
    assert slept == [rl.MAX_SLEEP]


def test_wait_raises_when_exhausted_and_reset_beyond_max_sleep(monkeypatch):
    """The production pathology: 0 remaining, reset 1091s out, MAX_SLEEP=60.

    Sleeping a capped 60s then firing produced a guaranteed 403 -- observed as a
    sleep/403 loop from 09:49 to 09:51 on 2026-10-28. It must fail fast instead of
    paying a minute to earn the same error.
    """
    now = 1_800_000_000.0
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type("R", (), {"json": lambda self: _payload(0, now + 1091)})(),
    )
    slept = []
    with pytest.raises(rl.RateLimitExhausted) as exc:
        rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=slept.append)
    assert slept == []  # no wasted sleep
    assert exc.value.remaining == 0
    assert exc.value.reset_in == pytest.approx(1091.0, abs=1.0)


def test_wait_sleeps_when_exhausted_but_reset_within_max_sleep(monkeypatch):
    """A short wait CAN bridge the reset, so sleeping is correct here -- do not trip."""
    now = 1_800_000_000.0
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type("R", (), {"json": lambda self: _payload(0, now + 5)})(),
    )
    slept = []
    r = rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=slept.append)
    assert r > 0
    assert len(slept) == 1


def test_exhaustion_propagates_through_client_gate():
    """The gate must not swallow RateLimitExhausted -- a budget check never breaks a
    call, but genuine exhaustion must surface so the caller fails fast."""
    import inspect

    from app import github_client as gc

    src = inspect.getsource(gc.GitHubClient._respect_budget)
    assert "except github_ratelimit.RateLimitExhausted:" in src
    assert src.index("except github_ratelimit.RateLimitExhausted:") < src.index(
        "except Exception:"
    )


def test_warn_threshold_logs_warning(monkeypatch, caplog):
    now = 1_800_000_000.0
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type(
            "R", (), {"json": lambda self: _payload(rl.WARN_BELOW - 1, now + 100)}
        )(),
    )
    with caplog.at_level("WARNING", logger="autopilot.github"):
        rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=lambda s: None)
    assert any("budget low" in r.getMessage() for r in caplog.records)
