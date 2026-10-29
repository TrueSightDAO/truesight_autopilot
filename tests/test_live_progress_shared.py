"""Live progress on shared session state (``session_state.progress_*``).

The point of this unit is NOT performance -- it is correctness across processes.
``/chat/progress`` is served to the Telegram adapter, which is a SEPARATE process
from the brain that runs the turn, so an in-process dict is always empty there.
These tests pin:

* default-OFF never constructs a Redis client and still round-trips locally;
* a record written by one "process" (one client) is visible to another;
* the record is scoped on the canonical conversation key, not the pubkey-prefixed
  session key -- the mismatch that silently emptied every snapshot;
* a finished turn clears the record, so a stale record cannot outlive its turn;
* Redis failure degrades instead of breaking the turn.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import session_state
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.session_state unavailable: {exc}", allow_module_level=True)


class FakeRedis:
    """Just the commands progress_* issues: SETEX / GET / DEL / SCAN."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.commands: list[tuple] = []

    def setex(self, key, ttl, value):
        self.commands.append(("setex", key, ttl))
        self.store[key] = value
        self.expiry[key] = int(ttl)
        return True

    def get(self, key):
        self.commands.append(("get", key))
        return self.store.get(key)

    def delete(self, key):
        self.commands.append(("delete", key))
        return 1 if self.store.pop(key, None) is not None else 0

    def scan_iter(self, match=None, count=None):
        self.commands.append(("scan_iter", match))
        prefix = (match or "").rstrip("*")
        for k in list(self.store):
            if k.startswith(prefix):
                yield k


class BrokenRedis:
    def __getattr__(self, name):
        def _boom(*a, **k):
            raise ConnectionError("redis is down")

        return _boom


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("SESSION_STATE_SHARED", raising=False)
    session_state._reset_for_tests()
    yield
    session_state._reset_for_tests()


@pytest.fixture
def shared(monkeypatch):
    """Turn sharing ON with a FakeRedis, without touching a live server."""
    monkeypatch.setenv("SESSION_STATE_SHARED", "1")
    fake = FakeRedis()
    monkeypatch.setattr(session_state, "_get_client", lambda: fake)
    return fake


TG_SCOPE = "tg:-1003919341801:21264"
DC_SCOPE = "dc:123456:789012"


# --------------------------------------------------------------------------- #
# default OFF
# --------------------------------------------------------------------------- #
def test_off_by_default_constructs_no_client(monkeypatch):
    """Unset env -> the local dict serves and Redis is never touched."""
    calls = []

    def _explode():
        calls.append(1)
        raise AssertionError("no Redis client may be built when sharing is off")

    monkeypatch.setattr(session_state, "_get_client", _explode)
    assert session_state.enabled() is False

    session_state.progress_set(TG_SCOPE, {"round": 3})
    assert session_state.progress_get(TG_SCOPE) == {"round": 3}
    session_state.progress_clear(TG_SCOPE)
    assert session_state.progress_get(TG_SCOPE) is None
    assert calls == []


# --------------------------------------------------------------------------- #
# cross-process visibility
# --------------------------------------------------------------------------- #
def test_record_written_by_turn_is_visible_to_a_second_reader(shared):
    session_state.progress_set(TG_SCOPE, {"round": 4, "current_tool": "web_search"})
    # A different process sharing the same Redis sees the same record.
    seen = session_state.progress_get(TG_SCOPE)
    assert seen == {"round": 4, "current_tool": "web_search"}
    assert any(c[0] == "setex" for c in shared.commands)


def test_snapshot_is_scoped_on_canonical_key_not_pubkey_session(shared):
    """The bug this fixes: turn writes <scope>, reader looked up pubkey:<scope>."""
    turn_scope = TG_SCOPE
    pubkey_prefixed = "abc123def456:" + TG_SCOPE
    session_state.progress_set(turn_scope, {"round": 1})

    assert session_state.progress_get(turn_scope) is not None
    assert session_state.progress_get(pubkey_prefixed) is None


def test_returns_a_copy_not_the_live_object(shared):
    session_state.progress_set(TG_SCOPE, {"round": 1})
    got = session_state.progress_get(TG_SCOPE)
    got["round"] = 99
    assert session_state.progress_get(TG_SCOPE) == {"round": 1}


def test_ttl_is_applied_so_a_stale_record_cannot_outlive_its_turn(shared):
    session_state.progress_set(TG_SCOPE, {"round": 1})
    key = session_state._progress_key(TG_SCOPE)
    assert shared.expiry[key] == session_state.PROGRESS_TTL_SECS


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_clear_removes_the_record(shared):
    session_state.progress_set(TG_SCOPE, {"round": 1})
    session_state.progress_clear(TG_SCOPE)
    assert session_state.progress_get(TG_SCOPE) is None
    assert ("delete", session_state._progress_key(TG_SCOPE)) in shared.commands


def test_scopes_lists_running_turns(shared):
    session_state.progress_set(TG_SCOPE, {"round": 1})
    session_state.progress_set(DC_SCOPE, {"round": 2})
    assert set(session_state.progress_scopes()) == {TG_SCOPE, DC_SCOPE}

    session_state.progress_clear(TG_SCOPE)
    assert session_state.progress_scopes() == [DC_SCOPE]


# --------------------------------------------------------------------------- #
# failure must not break a turn
# --------------------------------------------------------------------------- #
def test_redis_failure_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setenv("SESSION_STATE_SHARED", "1")
    monkeypatch.setattr(session_state, "_get_client", lambda: BrokenRedis())

    session_state.progress_set(TG_SCOPE, {"round": 7})  # must not raise
    assert session_state.progress_get(TG_SCOPE) == {"round": 7}
    session_state.progress_clear(TG_SCOPE)
    assert session_state.progress_get(TG_SCOPE) is None
