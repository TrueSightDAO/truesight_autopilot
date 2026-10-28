"""Tests for proactive GitHub rate-limit awareness (rate-limit plan PR2).

PR1 (#553) bounded *concurrency*. It cannot bound *budget*: N concurrent calls
still spend N of the hourly allowance. These tests pin the half PR1 cannot
cover -- read the budget before firing, warn when low, wait when critical, and
never let the check itself break a GitHub operation.
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
    # reset an hour away -> must be capped, never sleep the full hour
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: type("R", (), {"json": lambda self: _payload(0, now + 3600)})(),
    )
    slept = []
    r = rl.wait_if_exhausted("tok", now_fn=lambda: now, sleep_fn=slept.append)
    assert r == rl.MAX_SLEEP
    assert slept == [rl.MAX_SLEEP]


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
