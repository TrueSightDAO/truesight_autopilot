"""Shared session state (``app/session_state.py``): queue + cancel over Redis.

These pin the contract that makes it safe to move the message queue and cancel
flag off process-local memory:

* default-OFF touches no Redis at all (no client ever constructed);
* push / len / pop / snapshot / replace round-trip through the canonical key,
  and the key is scoped to the *conversation*, never to governor identity;
* ``queue_pop`` issues an atomic ``LPOP`` -- never read-then-delete, which would
  let two workers drain the same message;
* the cancel flag is set, observed and cleared, with a TTL;
* a Redis failure degrades to the process-local dicts instead of raising, so a
  Redis outage can never break a chat turn.

``FakeRedis`` emulates exactly the commands ``session_state`` issues, so no live
Redis and no new test dependency are needed.
"""

from __future__ import annotations

import pytest

from app import session_state

SCOPE = "tg:-1003919341801:21264"
OTHER = "dc:123456789:987654321"


class FakePipeline:
    """Buffers commands and applies them on ``execute()``."""

    def __init__(self, parent: "FakeRedis") -> None:
        self.parent = parent
        self.ops: list[tuple] = []

    def delete(self, key: str):
        self.ops.append(("delete", key, None))
        return self

    def rpush(self, key: str, value: str):
        self.ops.append(("rpush", key, value))
        return self

    def execute(self):
        if self.parent.down:
            raise ConnectionError("redis unreachable")
        for op, key, value in self.ops:
            if op == "delete":
                self.parent.store.pop(key, None)
            else:
                self.parent.store.setdefault(key, []).append(value)
        self.ops.clear()
        return [True] * len(self.ops)


class FakeRedis:
    """In-memory stand-in for the Redis commands ``session_state`` uses."""

    def __init__(self) -> None:
        self.store: dict[str, list | str | int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False
        self.calls: list[str] = []

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("redis unreachable")

    def rpush(self, key: str, value: str) -> int:
        self.calls.append("rpush")
        self._check()
        lst = self.store.setdefault(key, [])
        assert isinstance(lst, list)
        lst.append(value)
        return len(lst)

    def llen(self, key: str) -> int:
        self.calls.append("llen")
        self._check()
        lst = self.store.get(key) or []
        assert isinstance(lst, list)
        return len(lst)

    def lpop(self, key: str):
        self.calls.append("lpop")
        self._check()
        lst = self.store.get(key) or []
        assert isinstance(lst, list)
        return lst.pop(0) if lst else None

    def lrange(self, key: str, start: int, end: int) -> list:
        self.calls.append("lrange")
        self._check()
        lst = self.store.get(key) or []
        assert isinstance(lst, list)
        return list(lst) if end == -1 else list(lst[start : end + 1])

    def delete(self, key: str) -> int:
        self.calls.append("delete")
        self._check()
        existed = 1 if key in self.store else 0
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return existed

    def setex(self, key: str, ttl: int, value: str) -> bool:
        self.calls.append("setex")
        self._check()
        self.store[key] = value
        self.ttls[key] = int(ttl)
        return True

    def exists(self, key: str) -> int:
        self.calls.append("exists")
        self._check()
        return 1 if key in self.store else 0

    def pipeline(self) -> FakePipeline:
        self.calls.append("pipeline")
        return FakePipeline(self)


@pytest.fixture
def fake(monkeypatch):
    """Enable shared state against a FakeRedis, and reset between tests."""
    session_state._reset_for_tests()
    client = FakeRedis()
    monkeypatch.setenv("SESSION_STATE_SHARED", "1")
    monkeypatch.setattr(session_state, "_get_client", lambda: client)
    yield client
    session_state._reset_for_tests()


# --------------------------------------------------------------------------- #
# default OFF
# --------------------------------------------------------------------------- #
def test_disabled_by_default_never_constructs_a_client(monkeypatch):
    session_state._reset_for_tests()
    monkeypatch.delenv("SESSION_STATE_SHARED", raising=False)

    def boom():
        raise AssertionError("Redis client constructed while feature is off")

    monkeypatch.setattr(session_state, "_get_client", boom)

    assert not session_state.enabled()
    assert session_state.queue_push(SCOPE, {"id": "m1"}) == 1
    assert session_state.queue_len(SCOPE) == 1
    assert session_state.queue_pop(SCOPE) == {"id": "m1"}
    assert session_state.queue_pop(SCOPE) is None
    session_state.set_cancel(SCOPE)
    assert session_state.is_cancelled(SCOPE)
    session_state.clear_cancel(SCOPE)
    assert not session_state.is_cancelled(SCOPE)
    session_state._reset_for_tests()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_enabled_truthy_values(monkeypatch, value):
    session_state._reset_for_tests()
    monkeypatch.setenv("SESSION_STATE_SHARED", value)
    assert session_state.enabled()
    session_state._reset_for_tests()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
def test_enabled_falsy_values(monkeypatch, value):
    session_state._reset_for_tests()
    monkeypatch.setenv("SESSION_STATE_SHARED", value)
    assert not session_state.enabled()
    session_state._reset_for_tests()


# --------------------------------------------------------------------------- #
# key scheme -- conversation, never governor identity
# --------------------------------------------------------------------------- #
def test_keys_are_scoped_to_the_canonical_conversation(fake):
    session_state.queue_push(SCOPE, {"id": "m1"})
    session_state.set_cancel(SCOPE)

    assert fake.store.get(f"queue:{SCOPE}") == ['{"id": "m1"}']
    assert fake.store.get(f"cancel:{SCOPE}") == "1"


def test_queues_are_isolated_between_conversations(fake):
    session_state.queue_push(SCOPE, {"id": "a"})
    session_state.queue_push(OTHER, {"id": "b"})

    assert [m["id"] for m in session_state.queue_snapshot(SCOPE)] == ["a"]
    assert [m["id"] for m in session_state.queue_snapshot(OTHER)] == ["b"]

    # draing one conversation must not touch the other
    assert session_state.queue_pop(SCOPE) == {"id": "a"}
    assert session_state.queue_len(SCOPE) == 0
    assert session_state.queue_len(OTHER) == 1


def test_cancel_flags_are_isolated_between_conversations(fake):
    session_state.set_cancel(SCOPE)
    assert session_state.is_cancelled(SCOPE)
    assert not session_state.is_cancelled(OTHER)


# --------------------------------------------------------------------------- #
# queue semantics
# --------------------------------------------------------------------------- #
def test_push_returns_1_based_position(fake):
    assert session_state.queue_push(SCOPE, {"id": "m1"}) == 1
    assert session_state.queue_push(SCOPE, {"id": "m2"}) == 2
    assert session_state.queue_push(SCOPE, {"id": "m3"}) == 3


def test_pop_is_fifo(fake):
    for i in range(3):
        session_state.queue_push(SCOPE, {"id": f"m{i}"})
    assert [session_state.queue_pop(SCOPE)["id"] for _ in range(3)] == [
        "m0",
        "m1",
        "m2",
    ]
    assert session_state.queue_pop(SCOPE) is None


def test_pop_uses_atomic_lpop_not_read_then_delete(fake):
    """Two workers must not be able to drain the same message."""
    session_state.queue_push(SCOPE, {"id": "m1"})
    session_state.queue_push(SCOPE, {"id": "m2"})

    first = session_state.queue_pop(SCOPE)
    second = session_state.queue_pop(SCOPE)

    assert first != second
    assert "lpop" in fake.calls
    assert session_state.queue_pop(SCOPE) is None


def test_snapshot_does_not_consume(fake):
    session_state.queue_push(SCOPE, {"id": "m1"})
    session_state.queue_push(SCOPE, {"id": "m2"})

    assert [m["id"] for m in session_state.queue_snapshot(SCOPE)] == ["m1", "m2"]
    assert session_state.queue_len(SCOPE) == 2


def test_snapshot_and_pop_survive_unicode_payloads(fake):
    msg = {"id": "m1", "text": "cacao \u2014 10,000 hectares \U0001f33f"}
    session_state.queue_push(SCOPE, msg)
    assert session_state.queue_pop(SCOPE) == msg


def test_replace_overwrites_without_leaving_duplicates(fake):
    for i in range(3):
        session_state.queue_push(SCOPE, {"id": f"m{i}"})

    session_state.queue_replace(SCOPE, [{"id": "m1"}, {"id": "m2"}])

    assert [m["id"] for m in session_state.queue_snapshot(SCOPE)] == ["m1", "m2"]


def test_replace_with_empty_list_empties_the_queue(fake):
    session_state.queue_push(SCOPE, {"id": "m1"})
    session_state.queue_replace(SCOPE, [])
    assert session_state.queue_len(SCOPE) == 0


# --------------------------------------------------------------------------- #
# cancel semantics
# --------------------------------------------------------------------------- #
def test_cancel_sets_a_ttl(fake):
    session_state.set_cancel(SCOPE)
    assert fake.ttls.get(f"cancel:{SCOPE}") == session_state.CANCEL_TTL_SECS


def test_clear_cancel_drops_the_key(fake):
    session_state.set_cancel(SCOPE)
    session_state.clear_cancel(SCOPE)
    assert f"cancel:{SCOPE}" not in fake.store
    assert not session_state.is_cancelled(SCOPE)


def test_clear_cancel_is_idempotent(fake):
    session_state.clear_cancel(SCOPE)
    session_state.clear_cancel(SCOPE)
    assert not session_state.is_cancelled(SCOPE)


# --------------------------------------------------------------------------- #
# Redis failure must never break a turn
# --------------------------------------------------------------------------- #
def test_redis_outage_degrades_to_local_dicts(fake):
    fake.down = True

    assert session_state.queue_push(SCOPE, {"id": "m1"}) == 1
    assert session_state.queue_len(SCOPE) == 1
    assert session_state.queue_snapshot(SCOPE) == [{"id": "m1"}]
    assert session_state.queue_pop(SCOPE) == {"id": "m1"}
    assert session_state.queue_pop(SCOPE) is None

    session_state.set_cancel(SCOPE)
    assert session_state.is_cancelled(SCOPE)
    session_state.clear_cancel(SCOPE)
    assert not session_state.is_cancelled(SCOPE)


def test_redis_outage_on_replace_degrades_without_raising(fake):
    fake.down = True
    session_state.queue_replace(SCOPE, [{"id": "m1"}])
    assert session_state.queue_len(SCOPE) == 1


def test_redis_outage_warns_only_once(fake, caplog):
    fake.down = True
    with caplog.at_level("WARNING"):
        for _ in range(5):
            session_state.queue_len(SCOPE)
    warnings = [
        r for r in caplog.records if "shared session state unavailable" in r.message
    ]
    assert len(warnings) == 1
