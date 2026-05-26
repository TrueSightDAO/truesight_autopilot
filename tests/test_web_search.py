"""Unit tests for the Tavily-backed web_search / web_extract tools (httpx mocked)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.tools import web_search as ws


@pytest.fixture(autouse=True)
def _force_key(monkeypatch):
    """Pretend a Tavily key is configured for the duration of each test."""
    monkeypatch.setattr(ws.settings, "tavily_api_key", "tvly-test-key", raising=False)


def _mock_post(monkeypatch, body: dict, status: int = 200, capture: dict | None = None):
    def fake_post(url, json=None, timeout=None):  # noqa: A002 - match httpx.post signature
        if capture is not None:
            capture["url"] = url
            capture["payload"] = json
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(ws.httpx, "post", fake_post)


def test_web_search_success(monkeypatch):
    capture: dict = {}
    _mock_post(monkeypatch, {
        "answer": "A synthesized answer.",
        "results": [
            {"title": "Result One", "url": "https://example.com/1", "content": "snippet one", "score": 0.9},
            {"title": "Result Two", "url": "https://example.com/2", "content": "snippet two", "score": 0.8},
        ],
    }, capture=capture)

    out = json.loads(ws.web_search("ceremonial cacao", max_results=2))
    assert out["status"] == "ok"
    assert out["result_count"] == 2
    assert out["answer"] == "A synthesized answer."
    assert out["results"][0]["url"] == "https://example.com/1"
    # request shape
    assert capture["url"].endswith("/search")
    assert capture["payload"]["query"] == "ceremonial cacao"
    assert capture["payload"]["api_key"] == "tvly-test-key"
    assert capture["payload"]["max_results"] == 2


def test_web_search_clamps_max_results(monkeypatch):
    capture: dict = {}
    _mock_post(monkeypatch, {"results": []}, capture=capture)
    ws.web_search("x", max_results=999)
    assert capture["payload"]["max_results"] == 10  # clamped to 10


def test_web_search_empty_query():
    out = json.loads(ws.web_search("   "))
    assert out["status"] == "error"
    assert "query" in out["message"].lower()


def test_web_search_missing_key(monkeypatch):
    monkeypatch.setattr(ws.settings, "tavily_api_key", "", raising=False)
    out = json.loads(ws.web_search("anything"))
    assert out["status"] == "error"
    assert "TAVILY_API" in out["message"]


def test_web_search_http_error(monkeypatch):
    _mock_post(monkeypatch, {"detail": "bad"}, status=401)
    out = json.loads(ws.web_search("anything"))
    assert out["status"] == "error"
    assert "401" in out["message"]


def test_web_extract_success(monkeypatch):
    capture: dict = {}
    _mock_post(monkeypatch, {
        "results": [{"url": "https://example.com/1", "raw_content": "full page text"}],
        "failed_results": [],
    }, capture=capture)

    out = json.loads(ws.web_extract("https://example.com/1"))
    assert out["status"] == "ok"
    assert out["extracted_count"] == 1
    assert out["results"][0]["content"] == "full page text"
    assert capture["url"].endswith("/extract")
    assert capture["payload"]["urls"] == ["https://example.com/1"]


def test_web_extract_no_urls():
    out = json.loads(ws.web_extract([]))
    assert out["status"] == "error"
