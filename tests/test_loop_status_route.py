"""Route-level tests for GET /loop/status -- governor gate + degraded behaviour."""

import pytest
from fastapi.testclient import TestClient

from app import loop_surface


def _healthy_report():
    return {
        "generated_at": "2026-10-29T06:00:00Z",
        "total": 0,
        "counts": {},
        "defects": [],
        "daemon": {
            "heartbeat_age_s": 1.0,
            "stale": False,
            "heartbeat_stale_after_s": 180.0,
        },
        "healthy": True,
        "degraded": False,
        "error": None,
    }


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    monkeypatch.setattr(loop_surface, "build_report", lambda: _healthy_report())
    return TestClient(app)


def test_requires_public_key(client):
    r = client.get("/loop/status")
    assert r.status_code == 400
    assert "X-Public-Key" in r.json()["error"]


def test_rejects_non_governor(client, monkeypatch):
    monkeypatch.setattr("app.main.is_governor", lambda pk: False)
    r = client.get("/loop/status", headers={"X-Public-Key": "not-a-governor"})
    assert r.status_code == 403
    assert "Governor access required" in r.json()["error"]


def test_governor_gets_report(client, monkeypatch):
    monkeypatch.setattr("app.main.is_governor", lambda pk: True)
    r = client.get("/loop/status", headers={"X-Public-Key": "somekey"})
    assert r.status_code == 200
    body = r.json()
    assert body["healthy"] is True
    assert body["generated_at"].endswith("Z")


def test_degraded_report_still_returns_200(client, monkeypatch):
    """A Redis failure must not 500 -- it must be *visible* as degraded."""
    monkeypatch.setattr("app.main.is_governor", lambda pk: True)
    degraded = _healthy_report()
    degraded.update(
        healthy=False,
        degraded=True,
        error="redis unavailable: boom",
        daemon={
            "heartbeat_age_s": None,
            "stale": True,
            "heartbeat_stale_after_s": 180.0,
        },
    )
    monkeypatch.setattr(loop_surface, "build_report", lambda: degraded)
    r = client.get("/loop/status", headers={"X-Public-Key": "somekey"})
    assert r.status_code == 200
    assert r.json()["degraded"] is True
    assert "redis unavailable" in r.json()["error"]


def test_real_report_is_json_serialisable(monkeypatch):
    """Exercise the real builder through the route -- guards serialisation drift.

    Deliberately does NOT use the ``client`` fixture: that fixture stubs
    ``build_report``, so patching it again here would only ever test the stub.
    """
    import time as _time

    from app.main import app

    class FakeRedis:
        def scan_iter(self, pattern, count=None):
            yield "loop:status:a"

        def hgetall(self, key):
            return {
                "session_id": "a",
                "state": "lost_goal",
                "reason": "r",
                "age_s": "9",
            }

        def get(self, key):
            return str(int(_time.time()))

    monkeypatch.setattr("app.main.is_governor", lambda pk: True)
    monkeypatch.setattr("app.redis_claim.get_client", lambda: FakeRedis())

    r = TestClient(app).get("/loop/status", headers={"X-Public-Key": "somekey"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert [d["session_id"] for d in body["defects"]] == ["a"]
    assert body["counts"]["lost_goal"] == 1
    assert body["daemon"]["stale"] is False
