"""The per-session lock must bound the THREAD/CHANNEL, not the governor identity.

Regression guard for 2026-10-28: ``_session_lock_guard`` was handed
``session_id`` (``<pubkey[:20]>:<X-Session-Id>``), so mutual exclusion was
scoped to "this thread as seen by this governor" -- and renaming the governor
config entry quietly forked the lock namespace.

The contract these tests pin:

* an adapter ``X-Session-Id`` (``tg:<chat>:<thread>`` / ``dc:<guild>:<channel>``)
  is used verbatim as the lock scope;
* two different governors in one thread resolve to the SAME lock scope;
* anything else (web per-tab uuid, missing header) falls back to ``session_id``
  unchanged, so the web UI behaves exactly as before.
"""

from __future__ import annotations

from starlette.requests import Request

from app.main import _lock_scope, _session_key

PK_A = "04a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"
PK_B = "04ffffeeeedddccccbbbbaaaa9999888877776666555544443333222211110000"
TG_SID = "tg:-1003919341801:21264"
DC_SID = "dc:1098765432109876543:1122334455667788990"


def _req(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/chat", "headers": raw})


def test_telegram_thread_key_used_verbatim() -> None:
    req = _req({"X-Session-Id": TG_SID})
    assert _lock_scope(req, _session_key(PK_A, req)) == TG_SID


def test_discord_channel_key_used_verbatim() -> None:
    req = _req({"X-Session-Id": DC_SID})
    assert _lock_scope(req, _session_key(PK_A, req)) == DC_SID


def test_two_governors_same_thread_share_one_lock_scope() -> None:
    """The whole point: lock scope must not vary with governor identity."""
    req = _req({"X-Session-Id": TG_SID})
    scope_a = _lock_scope(req, _session_key(PK_A, req))
    scope_b = _lock_scope(req, _session_key(PK_B, req))
    assert scope_a == scope_b == TG_SID
    # ...while the transcript identity still differs, as it must.
    assert _session_key(PK_A, req) != _session_key(PK_B, req)


def test_pubkey_prefix_is_absent_from_lock_scope() -> None:
    req = _req({"X-Session-Id": TG_SID})
    scope = _lock_scope(req, _session_key(PK_A, req))
    assert PK_A[:20] not in scope
    assert not scope.startswith(PK_A[:20])


def test_web_tab_session_falls_back_to_session_id() -> None:
    """Web UI sends a per-tab uuid; behaviour must be unchanged."""
    req = _req({"X-Session-Id": "9f2c41d8e7ab"})
    sid = _session_key(PK_A, req)
    assert _lock_scope(req, sid) == sid


def test_missing_header_falls_back_to_session_id() -> None:
    req = _req()
    sid = _session_key(PK_A, req)
    assert _lock_scope(req, sid) == sid


def test_telegram_general_topic_thread_id_zero_is_canonical() -> None:
    """``thread_id or 0`` is a real value, not a missing one."""
    sid = "tg:-1003919341801:0"
    req = _req({"X-Session-Id": sid})
    assert _lock_scope(req, _session_key(PK_A, req)) == sid


def test_malformed_adapter_key_falls_back() -> None:
    for bad in ("tg:only-two", "tg::21264", "tg:-100:21264:extra", "xx:1:2"):
        req = _req({"X-Session-Id": bad})
        sid = _session_key(PK_A, req)
        assert _lock_scope(req, sid) == sid, bad
