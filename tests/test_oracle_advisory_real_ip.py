"""Regression test for the oracle-advisory rate-limit IP bug (2026-09-07).

nginx proxies /oracle-advisory to 127.0.0.1:8001, so request.client.host is
ALWAYS the loopback address -- every visitor collapsed into one rate-limit
bucket, effectively making "1 req per 2s per IP" a GLOBAL limit across every
oracle user. Found live: a completely fresh browser's first /oracle-advisory
call got 429'd (unrelated traffic had hit the same 127.0.0.1 bucket).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.main import _real_client_ip


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict[str, str], client_host: str = "127.0.0.1") -> None:
        self.headers = headers
        self.client = _FakeClient(client_host)


def test_prefers_x_forwarded_for():
    req = _FakeRequest({"x-forwarded-for": "203.0.113.5, 127.0.0.1"})
    assert _real_client_ip(req) == "203.0.113.5"


def test_falls_back_to_x_real_ip():
    req = _FakeRequest({"x-real-ip": "203.0.113.9"})
    assert _real_client_ip(req) == "203.0.113.9"


def test_falls_back_to_request_client_when_no_proxy_headers():
    req = _FakeRequest({}, client_host="203.0.113.1")
    assert _real_client_ip(req) == "203.0.113.1"


def test_two_different_forwarded_ips_are_not_conflated():
    """The actual bug: before the fix, every visitor shared one bucket
    (request.client.host == '127.0.0.1' behind nginx). After the fix, two
    different real visitors must resolve to two different IPs."""
    req_a = _FakeRequest({"x-forwarded-for": "203.0.113.5"})
    req_b = _FakeRequest({"x-forwarded-for": "198.51.100.7"})
    assert _real_client_ip(req_a) != _real_client_ip(req_b)


def test_unknown_when_nothing_available():
    req = SimpleNamespace(headers={}, client=None)
    assert _real_client_ip(req) == "unknown"
