"""PR-A -- per-worker registry, heartbeat and session->worker affinity.

These pin the properties that make ``app/dispatch_registry.py`` safe to land
behind a default-OFF flag:

* default-OFF never touches Redis (no client is even constructed);
* a heartbeat records identity AND index membership, with a TTL on the record;
* the heartbeat is deliberately NOT singleton-guarded -- a registry listing one
  worker out of four is worse than no registry, so we assert no ``singleton:``
  key is created;
* a dead worker stays *visible as dead* until ``prune_stale`` reaps it;
* affinity round-trips with a long TTL and is written off the event loop;
* nothing may raise into a turn or an endpoint: Redis errors degrade to a falsy
  value, and the heartbeat loop survives a failed beat;
* the public projection is an allowlist -- ``worker_id`` (host:pid) never
  leaves the process, only an 8-char ``worker_ref``.

These run against a REAL local Redis (the same one ``redis_claim`` uses) and
skip cleanly when it is unreachable, so CI without Redis stays green.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import dispatch_registry as dr
    from app import redis_claim
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app imports unavailable: {exc}", allow_module_level=True)


def _wid(tag: str = "a") -> str:
    """A unique, obviously-fake worker id (never a real host:pid)."""
    return f"testhost-{uuid.uuid4().hex[:8]}:{tag}"


@pytest.fixture(scope="module")
def client():
    """A live Redis client, or skip the whole module."""
    try:
        c = redis_claim.get_client()
        c.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Redis unavailable: {exc}")
    yield c
    c.close()


@pytest.fixture
def on(monkeypatch, client):
    """Registry enabled, module test state reset, keys reaped afterwards."""
    monkeypatch.setenv("DISPATCH_REGISTRY_ENABLED", "1")
    dr._reset_for_tests()
    seen: list[str] = []
    yield seen
    for wid in seen:
        client.delete(dr.worker_key(wid))
        client.zrem(dr.WORKERS_INDEX, wid)


def _track(seen: list[str], *wids: str) -> None:
    seen.extend(wids)


# --------------------------------------------------------------------------- #
# flag + key scheme
# --------------------------------------------------------------------------- #
def test_enabled_default_off(monkeypatch):
    monkeypatch.delenv("DISPATCH_REGISTRY_ENABLED", raising=False)
    assert dr.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_enabled_accepts_truthy_values(monkeypatch, value):
    monkeypatch.setenv("DISPATCH_REGISTRY_ENABLED", value)
    assert dr.enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "nope"])
def test_enabled_rejects_other_values(monkeypatch, value):
    monkeypatch.setenv("DISPATCH_REGISTRY_ENABLED", value)
    assert dr.enabled() is False


def test_worker_and_affinity_key_formats():
    assert dr.worker_key("h:1") == "sophia:worker:h:1"
    assert dr.affinity_key("tg:-1:2") == "sophia:affinity:tg:-1:2"
    assert dr.WORKERS_INDEX == "sophia:workers"


# --------------------------------------------------------------------------- #
# worker_ref -- public-safe
# --------------------------------------------------------------------------- #
def test_worker_ref_is_short_hex_and_stable():
    ref = dr.worker_ref("host-abc:4242")
    assert len(ref) == 8
    assert int(ref, 16) >= 0  # hex
    assert ref == dr.worker_ref("host-abc:4242")


def test_worker_ref_hides_host_and_pid():
    wid = "ip-172-31-15-182:998877"
    ref = dr.worker_ref(wid)
    assert "ip-172" not in ref and "998877" not in ref and ":" not in ref
    assert ref != dr.worker_ref("ip-172-31-15-182:998878")


def test_worker_ref_distinct_for_distinct_workers():
    assert dr.worker_ref("a:1") != dr.worker_ref("a:2")


# --------------------------------------------------------------------------- #
# _decode
# --------------------------------------------------------------------------- #
def test_decode_parses_valid_record():
    assert dr._decode('{"beat_at": 5}') == {"beat_at": 5}


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", "42"])
def test_decode_rejects_none_and_garbage(raw):
    assert dr._decode(raw) is None


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("DISPATCH_REGISTRY_ENABLED", raising=False)
    dr._reset_for_tests()

    def boom():  # a client must never be constructed while off
        raise AssertionError("get_client() called while disabled")

    monkeypatch.setattr(redis_claim, "get_client", boom)
    assert dr.heartbeat("h:1") is False
    assert dr.prune_stale() == 0
    assert dr.workers() == []
    assert dr.worker_count() == {"total": 0, "alive": 0, "dead": 0}
    assert dr.set_affinity("s") is False
    assert dr.affinity_owner("s") is None
    assert dr.clear_affinity("s") is False


def test_heartbeat_writes_record_and_index(on, client):
    wid = _wid()
    _track(on, wid)
    assert dr.heartbeat(wid, client, now=1000.0) is True

    record = dr._decode(client.get(dr.worker_key(wid)))
    assert record is not None
    assert record["worker_id"] == wid
    assert record["beat_at"] == 1000.0
    assert record["pid"] == os.getpid()
    assert record["host"]

    score = client.zscore(dr.WORKERS_INDEX, wid)
    assert score == 1000.0


def test_heartbeat_record_carries_ttl(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client)
    ttl = client.ttl(dr.worker_key(wid))
    assert 0 < ttl <= dr.HEARTBEAT_TTL_SECS


def test_heartbeat_first_beat_is_sticky(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client, now=100.0)
    dr.heartbeat(wid, client, now=130.0)
    record = dr._decode(client.get(dr.worker_key(wid)))
    assert record["first_beat_at"] == 100.0
    assert record["beat_at"] == 130.0


def test_heartbeat_merges_extra(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client, extra={"roles": ["chat", "worker"]})
    record = dr._decode(client.get(dr.worker_key(wid)))
    assert record["roles"] == ["chat", "worker"]


def test_heartbeat_swallows_redis_error(on):
    class Broken:
        def pipeline(self):
            raise RuntimeError("redis down")

    assert dr.heartbeat(_wid(), Broken()) is False
    assert dr._warned is True  # logged once, not raised


def test_heartbeat_creates_no_singleton_key(on, client):
    """``heartbeat_loop`` is un-guarded BY DESIGN -- it must not claim a singleton."""
    before = set(client.scan_iter(match="singleton:*", count=1000))
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client)
    after = set(client.scan_iter(match="singleton:*", count=1000))
    assert after == before


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def test_workers_sorts_alive_first(on, client):
    live, stale = _wid("live"), _wid("stale")
    _track(on, live, stale)
    dr.heartbeat(live, client, now=1000.0)
    dr.heartbeat(stale, client, now=900.0)  # dead relative to now=1000

    rows = [
        r for r in dr.workers(client, now=1000.0) if r["worker_id"] in (live, stale)
    ]
    assert [r["worker_id"] for r in rows] == [live, stale]
    assert rows[0]["alive"] is True
    assert rows[1]["alive"] is False


def test_workers_reports_dead_past_ttl(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client, now=1000.0)
    row = next(r for r in dr.workers(client, now=1000.0 + 31) if r["worker_id"] == wid)
    assert row["alive"] is False
    assert row["age_secs"] == 31.0


def test_workers_survives_missing_record(on, client):
    """Index membership can outlive the TTL'd record -- that is the design."""
    wid = _wid()
    _track(on, wid)
    client.zadd(dr.WORKERS_INDEX, {wid: 1000.0})
    row = next(r for r in dr.workers(client, now=1000.0) if r["worker_id"] == wid)
    assert row["alive"] is True  # falls back to the index score
    assert row["first_beat_at"] is None


def test_prune_stale_reaps_only_old(on, client):
    old, fresh = _wid("old"), _wid("fresh")
    _track(on, old, fresh)
    dr.heartbeat(old, client, now=1000.0)
    dr.heartbeat(fresh, client, now=1400.0)

    removed = dr.prune_stale(client, now=1400.0)
    assert removed >= 1
    members = client.zrange(dr.WORKERS_INDEX, 0, -1)
    assert old not in members
    assert fresh in members


def test_worker_count_shape(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client, now=1000.0)
    counts = dr.worker_count(client)
    assert set(counts) == {"total", "alive", "dead"}
    assert counts["total"] >= 1
    assert counts["total"] == counts["alive"] + counts["dead"]


# --------------------------------------------------------------------------- #
# affinity
# --------------------------------------------------------------------------- #
def test_affinity_set_get_clear_roundtrip(on, client):
    sid = f"tg:-100999:{uuid.uuid4().hex[:6]}"
    wid = _wid()
    _track(on, wid)
    try:
        assert dr.affinity_owner(sid, client) is None
        assert dr.set_affinity(sid, wid, client) is True
        assert dr.affinity_owner(sid, client) == wid
        assert dr.clear_affinity(sid, client) is True
        assert dr.affinity_owner(sid, client) is None
        assert dr.clear_affinity(sid, client) is False  # already gone
    finally:
        client.delete(dr.affinity_key(sid))


def test_affinity_has_ttl(on, client):
    sid = f"tg:-100999:{uuid.uuid4().hex[:6]}"
    wid = _wid()
    _track(on, wid)
    try:
        dr.set_affinity(sid, wid, client)
        ttl = client.ttl(dr.affinity_key(sid))
        assert 0 < ttl <= dr.AFFINITY_TTL_SECS
        assert ttl > dr.HEARTBEAT_TTL_SECS  # long-lived on purpose
    finally:
        client.delete(dr.affinity_key(sid))


def test_affinity_survives_redis_error(on):
    class Broken:
        def set(self, *a, **k):
            raise RuntimeError("redis down")

    assert dr.set_affinity("s", "w", Broken()) is False
    assert dr._warned is True


def test_note_session_accepted_writes_affinity(on, client):
    sid = f"tg:-100999:{uuid.uuid4().hex[:6]}"
    wid = _wid()
    _track(on, wid)

    async def run() -> None:
        dr.note_session_accepted(sid)
        await asyncio.sleep(0.2)  # let the fire-and-forget task land

    try:
        asyncio.run(run())
        assert dr.affinity_owner(sid, client) == dr.default_worker_id()
    finally:
        client.delete(dr.affinity_key(sid))


def test_note_session_accepted_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("DISPATCH_REGISTRY_ENABLED", raising=False)
    dr._reset_for_tests()

    async def run() -> None:
        dr.note_session_accepted("tg:-1:1")  # must not schedule anything

    asyncio.run(run())  # no exception, no Redis


# --------------------------------------------------------------------------- #
# public projection
# --------------------------------------------------------------------------- #
def test_public_worker_projects_allowlist_only():
    record = {
        "worker_id": "secret-host:1234",
        "alive": True,
        "age_secs": 1.0,
        "beat_at": 5.0,
        "first_beat_at": 1.0,
        "host": "secret-host",
        "pid": 1234,
        "roles": ["chat"],
        "future_field": "must not leak",
    }
    out = dr._public_worker(record)
    assert set(out) == set(dr.PUBLIC_WORKER_FIELDS)
    assert out["worker_ref"] == dr.worker_ref("secret-host:1234")
    assert "worker_id" not in out
    assert "future_field" not in out
    assert "secret-host" not in repr(out)


def test_snapshot_disabled_shape(monkeypatch):
    monkeypatch.delenv("DISPATCH_REGISTRY_ENABLED", raising=False)
    dr._reset_for_tests()
    snap = dr.snapshot()
    assert snap["enabled"] is False
    assert snap["workers"] == []
    assert snap["degraded"] is False
    assert "DISPATCH_REGISTRY_ENABLED" in snap["reason"]


def test_snapshot_live_hides_worker_id(on, client):
    wid = _wid()
    _track(on, wid)
    dr.heartbeat(wid, client, now=1000.0)
    snap = dr.snapshot(client, now=1000.0)

    assert snap["enabled"] is True
    assert snap["degraded"] is False
    assert snap["heartbeat_ttl_secs"] == dr.HEARTBEAT_TTL_SECS
    assert snap["worker_count"] == len(snap["workers"])
    assert snap["alive"] + snap["dead"] == snap["worker_count"]
    mine = next(w for w in snap["workers"] if w["worker_ref"] == dr.worker_ref(wid))
    assert "worker_id" not in mine
    assert wid not in repr(snap)


def test_snapshot_degraded_on_error(on):
    class Broken:
        def zrange(self, *a, **k):
            raise RuntimeError("redis down")

    snap = dr.snapshot(Broken())
    assert snap["enabled"] is True
    assert snap["degraded"] is True
    assert snap["workers"] == []
    assert snap["reason"] == "dispatch registry unavailable"


# --------------------------------------------------------------------------- #
# heartbeat loop
# --------------------------------------------------------------------------- #
def test_heartbeat_loop_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("DISPATCH_REGISTRY_ENABLED", raising=False)
    dr._reset_for_tests()
    asyncio.run(asyncio.wait_for(dr.heartbeat_loop(interval=0.01), timeout=1.0))


def test_heartbeat_loop_survives_beat_failure(on, monkeypatch):
    """A Redis hiccup must not kill the ticker -- the worker would vanish forever."""
    calls: list[int] = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("redis hiccup")
        return True

    monkeypatch.setattr(dr, "heartbeat", flaky)

    async def run() -> None:
        task = asyncio.create_task(dr.heartbeat_loop(interval=1.0))
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) >= 2  # first beat raised, loop kept going
