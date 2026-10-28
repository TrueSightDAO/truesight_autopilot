"""Plan SOPHIA_SIDEKIQ_MULTIPROCESS_PLAN (PR0) -- Discord dispatch mark.

Discord half of the Redis *mark* that already shipped on the Telegram side
(``ed56348``, #555). Mirrors ``tests/test_telegram_dispatch_mark.py``; the spec's
requirement is two-sided, so the tests are too:

  1. a mark now exists in Redis for a dispatched Discord channel, AND
  2. existing dispatch behaviour is provably unchanged -- the mark must not gate
     or alter a turn (a Redis outage must not stop a message being handled), and
     a message that is *not* dispatched must not be marked (a mark is an
     invitation to work, so it must never point at work nobody can do).

Session ids here are ``dc:<guild>:<channel>`` (see ``build_session_id``), the
key space PR2's idle worker will discover alongside ``tg:`` sessions.
"""

from __future__ import annotations

import pytest

from app import discord_adapter as da

DISPATCHED_SESSION = "dc:1:555"


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


def _msg(user_id="999", content="hello", bot=False, channel="555"):
    return {
        "id": "msg1",
        "channel_id": channel,
        "content": content,
        "author": {"id": user_id, "username": "u", "bot": bot},
    }


def _patch_dispatch(monkeypatch, reply="reply!"):
    """Neutralise everything around the turn so we observe the mark, not Discord."""
    sent: dict = {}
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "call_chat_with_progress", lambda *a, **k: (reply, False))
    monkeypatch.setattr(
        da, "send_message", lambda ch, txt: sent.update(ch=ch, txt=txt) or []
    )
    monkeypatch.setattr(da, "add_reaction", lambda *a, **k: True)
    monkeypatch.setattr(da, "remove_reaction", lambda *a, **k: True)
    return sent


# --- the mark itself --------------------------------------------------------


def test_record_dispatch_mark_writes_mark(fake_redis):
    """The helper produces a real mark for a Discord session key."""
    from app import redis_claim as rc

    da._record_dispatch_mark(DISPATCHED_SESSION)

    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is True
    assert fake_redis.zadd_calls, "expected a zadd against the marks key"
    assert list(fake_redis.zadd_calls[0][1]) == [DISPATCHED_SESSION]


def test_mark_is_idempotent(fake_redis):
    """Re-marking refreshes the timestamp instead of adding a duplicate member."""
    from app import redis_claim as rc

    da._record_dispatch_mark(DISPATCHED_SESSION)
    da._record_dispatch_mark(DISPATCHED_SESSION)

    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is True
    assert len(fake_redis.scores) == 1, "mark must not duplicate per inbound message"


def test_mark_failure_never_breaks_dispatch(monkeypatch):
    """A Redis outage must not prevent the adapter handling a message.

    Load-bearing half of "additive only": if Redis is down, marking raises and
    the helper must swallow it so dispatch proceeds untouched.
    """
    from app import redis_claim as rc

    def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rc, "get_client", _boom, raising=False)

    da._record_dispatch_mark(DISPATCHED_SESSION)  # must not raise


def test_mark_failure_from_blocked_module_never_breaks_dispatch(monkeypatch):
    """Even an import-level failure (redis package absent) is swallowed."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name == "redis":
            raise ModuleNotFoundError("No module named 'redis'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    da._record_dispatch_mark(DISPATCHED_SESSION)  # must not raise


def test_mark_is_not_on_the_dispatch_decision_path():
    """Guard the invariant structurally: marking cannot gate a turn.

    The helper returns None and takes only a session id, so no caller can branch
    on it -- it cannot become a dispatch condition by accretion without someone
    deliberately changing its signature.
    """
    import inspect

    sig = inspect.signature(da._record_dispatch_mark)
    assert (sig.return_annotation in (None, "None")) or sig.return_annotation is None
    assert list(sig.parameters) == ["session_id"]


# --- wired into handle_message ---------------------------------------------


def test_handle_message_marks_dispatched_channel(monkeypatch, fake_redis):
    """A dispatched governor message leaves a mark for its ``dc:`` session."""
    from app import redis_claim as rc

    sent = _patch_dispatch(monkeypatch)
    da.handle_message(_msg(content="<@42> do a thing"), {"999"}, "KEY", "1", "42")

    assert sent["txt"] == "reply!"  # turn still ran exactly as before
    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is True


def test_handle_message_marks_member_mention_dispatch(monkeypatch, fake_redis):
    """The member @-mention dispatch path marks too -- any turn is real work."""
    from app import redis_claim as rc

    sent = _patch_dispatch(monkeypatch)
    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "member")
    da.handle_message(_msg(content="<@42> what is our yield?"), set(), "KEY", "1", "42")

    assert sent["txt"] == "reply!"
    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is True


def test_handle_message_does_not_mark_observed_only(monkeypatch, fake_redis):
    """Context-only chatter is not unprocessed work -- and must not be marked.

    A mark invites a worker to act; marking a message we deliberately do not
    dispatch would hand it an invitation it could never honour.
    """
    from app import redis_claim as rc

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "guest")
    monkeypatch.setattr(da, "log_observed_message", lambda *a, **k: None)
    monkeypatch.setattr(
        da,
        "call_chat_with_progress",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guest dispatched!")),
    )
    da.handle_message(_msg(), set(), "KEY", "1", "42")

    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is False
    assert not fake_redis.zadd_calls


def test_handle_message_bot_message_not_marked(monkeypatch, fake_redis):
    """A dropped bot message never reaches the mark."""
    from app import redis_claim as rc

    monkeypatch.setattr(da.settings, "discord_trusted_bot_ids", "111")
    monkeypatch.setattr(da, "send_message", lambda *a, **k: [])
    da.handle_message(_msg(user_id="42", bot=True), {"999"}, "KEY", "1", "42")

    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is False


def test_handle_message_without_public_key_not_marked(monkeypatch, fake_redis):
    """No brain key means no turn runs -- so no mark (mark sits after that gate)."""
    from app import redis_claim as rc

    monkeypatch.setattr(da, "author_role", lambda uid, allowed: "governor")
    monkeypatch.setattr(da, "send_message", lambda *a, **k: [])
    da.handle_message(_msg(), {"999"}, None, "1", "42")

    assert rc.is_marked(DISPATCHED_SESSION, client=fake_redis) is False


def test_handle_message_survives_redis_down_and_still_replies(monkeypatch):
    """End-to-end 'additive only': Redis dead, governor still gets an answer."""
    from app import redis_claim as rc

    def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rc, "get_client", _boom, raising=False)
    sent = _patch_dispatch(monkeypatch, reply="still works")

    da.handle_message(_msg(content="<@42> do a thing"), {"999"}, "KEY", "1", "42")

    assert sent["txt"] == "still works"
