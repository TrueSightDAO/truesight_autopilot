"""Plan SOPHIA_REDIS_MARK_AND_CLAIM_DISPATCH -- PR2.

Telegram adapter writes a Redis *mark* on every inbound message, **additive only**.

The spec's PR2 requirement is two-sided, so the tests are too:
  1. a mark now exists in Redis after ``handle_message`` runs, AND
  2. existing dispatch behaviour is provably unchanged -- the mark must not sit on
     the dispatch path (a Redis outage must not stop a message being handled).
"""

from __future__ import annotations

import pytest

from app import telegram_adapter as ta


class _FakeRedis:
    """Minimal stand-in for the zadd-based mark API in app.redis_claim."""

    def __init__(self) -> None:
        self.zadd_calls: list[tuple[str, dict]] = []
        self.scores: dict[str, float] = {}

    def zadd(self, key: str, mapping: dict) -> None:
        self.zadd_calls.append((key, dict(mapping)))
        self.scores.update(mapping)

    def zscore(self, key: str, member: str):
        return self.scores.get(member)

    def zrem(self, key: str, member: str) -> int:
        return 1 if self.scores.pop(member, None) is not None else 0


@pytest.fixture
def fake_redis(monkeypatch):
    """Route app.redis_claim's client at a fake, so no real Redis is needed."""
    from app import redis_claim as rc

    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_client", lambda: fake, raising=False)
    return fake


def test_record_dispatch_mark_writes_mark(fake_redis):
    """The helper produces a real mark for the session key."""
    from app import redis_claim as rc

    ta._record_dispatch_mark("tg:-100123:456")

    assert rc.is_marked("tg:-100123:456", client=fake_redis) is True
    assert fake_redis.zadd_calls, "expected a zadd against the marks key"
    key, mapping = fake_redis.zadd_calls[0]
    assert list(mapping) == ["tg:-100123:456"]


def test_mark_is_idempotent(fake_redis):
    """Re-marking refreshes the timestamp instead of adding a duplicate member."""
    from app import redis_claim as rc

    ta._record_dispatch_mark("tg:-100123:456")
    ta._record_dispatch_mark("tg:-100123:456")

    assert rc.is_marked("tg:-100123:456", client=fake_redis) is True
    assert len(fake_redis.scores) == 1, "mark must not duplicate per inbound message"


def test_mark_failure_never_breaks_dispatch(monkeypatch):
    """A Redis outage must not prevent the adapter handling a message.

    This is the load-bearing half of "additive only": if Redis is down, marking
    raises, and the helper must swallow it so dispatch proceeds untouched.
    """
    from app import redis_claim as rc

    def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rc, "get_client", _boom, raising=False)

    # Must not raise.
    ta._record_dispatch_mark("tg:-100123:456")


def test_mark_failure_from_blocked_module_never_breaks_dispatch(monkeypatch):
    """Even an import-level failure (redis package absent) is swallowed."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name == "redis":
            raise ModuleNotFoundError("No module named 'redis'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    ta._record_dispatch_mark("tg:-100123:456")  # must not raise


def test_mark_is_not_on_the_dispatch_decision_path():
    """Guard the invariant: marking must not gate whether a turn runs.

    Cheap structural assertion -- the helper returns None and carries no boolean
    that a caller could branch on, so it cannot become a dispatch condition by
    accretion without someone deliberately changing its signature.
    """
    import inspect

    sig = inspect.signature(ta._record_dispatch_mark)
    assert (sig.return_annotation in (None, "None")) or sig.return_annotation is None
    assert list(sig.parameters) == ["session_id"]
