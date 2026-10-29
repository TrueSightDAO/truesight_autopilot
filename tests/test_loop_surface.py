"""Tests for the /loop/status read surface (U3)."""

import pytest

from app import loop_surface


class FakeRedis:
    """Minimal surface used by loop_surface: scan_iter / hgetall / get."""

    def __init__(self, hashes=None, heartbeat=None, fail_scan=False, fail_get=False):
        self.hashes = hashes or {}
        self.value = heartbeat
        self.fail_scan = fail_scan
        self.fail_get = fail_get

    def scan_iter(self, pattern, count=None):
        if self.fail_scan:
            raise RuntimeError("scan boom")
        prefix = pattern.rstrip("*")
        for key in sorted(self.hashes):
            if key.startswith(prefix):
                yield key

    def hgetall(self, key):
        return dict(self.hashes[key])

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("get boom")
        return self.value


def _hash(**over):
    base = {
        "session_id": "tg:-100:1",
        "state": "lost_goal",
        "reason": "open goal, nothing running",
        "age_s": "120",
        "label": "Work thread",
        "goal": "Ship U3",
    }
    base.update(over)
    return base


def test_parse_status_hash_roundtrip():
    s = loop_surface.parse_status_hash(_hash())
    assert s is not None
    assert s.session_id == "tg:-100:1"
    assert s.state == "lost_goal"
    assert s.age_s == 120.0
    assert s.goal == "Ship U3"


@pytest.mark.parametrize(
    "raw", [None, "notadict", {}, {"session_id": "x"}, {"state": "idle"}]
)
def test_parse_status_hash_rejects_unusable(raw):
    assert loop_surface.parse_status_hash(raw) is None


def test_parse_status_hash_tolerates_bad_age():
    s = loop_surface.parse_status_hash(_hash(age_s="not-a-number"))
    assert s is not None
    assert s.age_s is None


def test_read_statuses_sorted_and_skips_corrupt():
    client = FakeRedis(
        {
            "loop:status:b": _hash(session_id="b"),
            "loop:status:a": _hash(session_id="a"),
            "loop:status:bad": {"nonsense": "1"},
        }
    )
    got = loop_surface.read_statuses(client)
    assert [s.session_id for s in got] == ["a", "b"]


def test_read_statuses_survives_one_unreadable_key():
    class Partial(FakeRedis):
        def hgetall(self, key):
            if key.endswith("boom"):
                raise RuntimeError("nope")
            return super().hgetall(key)

    client = Partial(
        {"loop:status:boom": _hash(), "loop:status:ok": _hash(session_id="ok")}
    )
    got = loop_surface.read_statuses(client)
    assert [s.session_id for s in got] == ["ok"]


def test_heartbeat_age():
    assert loop_surface.heartbeat_age(
        FakeRedis(heartbeat="1000"), now=1120
    ) == pytest.approx(120)
    assert loop_surface.heartbeat_age(FakeRedis(heartbeat=None), now=1120) is None
    assert loop_surface.heartbeat_age(FakeRedis(heartbeat="garbage"), now=1120) is None
    assert loop_surface.heartbeat_age(FakeRedis(heartbeat="2000"), now=1120) == 0.0


def test_build_report_healthy():
    client = FakeRedis(
        {"loop:status:a": _hash(session_id="a", state="running")}, heartbeat="1000"
    )
    report = loop_surface.build_report(client, now=1010)
    assert report["healthy"] is True
    assert report["degraded"] is False
    assert report["total"] == 1
    assert report["defects"] == []
    assert report["daemon"]["stale"] is False
    assert report["daemon"]["heartbeat_age_s"] == pytest.approx(10)


def test_build_report_lists_defects():
    client = FakeRedis(
        {
            "loop:status:a": _hash(session_id="a", state="lost_goal"),
            "loop:status:b": _hash(session_id="b", state="stalled"),
            "loop:status:c": _hash(session_id="c", state="running"),
        },
        heartbeat="1000",
    )
    report = loop_surface.build_report(client, now=1010)
    assert report["healthy"] is False
    ids = {d["session_id"] for d in report["defects"]}
    assert ids == {"a", "b"}
    assert report["counts"]["lost_goal"] == 1
    assert report["counts"]["stalled"] == 1


def test_build_report_empty_statuses_is_not_healthy():
    """A silent daemon with no verdicts is the most misleading state -- flag it."""
    report = loop_surface.build_report(FakeRedis({}, heartbeat=None), now=1010)
    assert report["healthy"] is False
    assert report["daemon"]["stale"] is True
    assert report["daemon"]["heartbeat_age_s"] is None


def test_build_report_stale_heartbeat_not_healthy():
    report = loop_surface.build_report(
        FakeRedis(
            {"loop:status:a": _hash(session_id="a", state="running")}, heartbeat="1000"
        ),
        now=1000 + loop_surface.DEFAULT_HEARTBEAT_STALE_S + 1,
    )
    assert report["daemon"]["stale"] is True
    assert report["healthy"] is False


def test_build_report_degrades_on_redis_error():
    report = loop_surface.build_report(FakeRedis(fail_scan=True), now=1010)
    assert report["degraded"] is True
    assert report["healthy"] is False
    assert "status read failed" in (report["error"] or "")


def test_build_report_survives_heartbeat_failure():
    client = FakeRedis(
        {"loop:status:a": _hash(session_id="a", state="running")}, fail_get=True
    )
    report = loop_surface.build_report(client, now=1010)
    assert report["daemon"]["stale"] is True
    assert report["healthy"] is False


def test_build_report_does_not_raise_when_redis_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no redis")

    monkeypatch.setattr("app.redis_claim.get_client", boom)
    report = loop_surface.build_report(None, now=1010)
    assert report["degraded"] is True
    assert "redis unavailable" in (report["error"] or "")
    assert report["generated_at"].endswith("Z")
