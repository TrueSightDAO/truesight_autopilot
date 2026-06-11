"""Tests for DeepSeekProvider — XML/DSML tool-call fallback, normal completion, cost estimation."""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm.base import LLMError, LLMUsage
from app.llm.deepseek import DeepSeekProvider


def _make_client(body: dict, status: int = 200) -> httpx.Client:
    """Return an httpx.Client with MockTransport that returns a fixed JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ── Test 1: Normal completion with standard tool_calls ──


def test_normal_completion_with_tool_calls():
    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Let me look that up.",
                    "tool_calls": [
                        {
                            "id": "call_001",
                            "type": "function",
                            "function": {
                                "name": "lookup_qr_code",
                                "arguments": '{"qr_code": "2024OSCAR_20260330_21"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = _make_client(body)

    resp = p.chat("You are helpful.", [{"role": "user", "content": "hi"}])

    assert resp.text == "Let me look that up."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "lookup_qr_code"
    assert resp.finish_reason == "tool_calls"
    assert resp.usage.prompt_tokens == 100
    assert resp.usage.completion_tokens == 50
    assert resp.usage.total_tokens == 150


# ── Test 2: Completion with XML tool_calls in content (DeepSeek quirk) ──


def test_xml_tool_calls_in_content():
    """DeepSeek sometimes emits tool calls as XML in content — parser should extract them."""
    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "I'll scan the file.\n\n"
                        "<function_calls>"
                        '<invoke name="scan_qr_from_file">'
                        '<parameter name="file_path">/tmp/test.jpg</parameter>'
                        "</invoke>"
                        "</function_calls>"
                    ),
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
    }
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = _make_client(body)

    resp = p.chat(
        "You are helpful.", [{"role": "user", "content": "scan /tmp/test.jpg"}]
    )

    # Content should be cleaned of XML
    assert "<function_calls>" not in resp.text
    assert "I'll scan the file." in resp.text
    # Tool calls should be extracted from XML
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "scan_qr_from_file"
    args = json.loads(resp.tool_calls[0]["function"]["arguments"])
    assert args["file_path"] == "/tmp/test.jpg"


# ── Test 3: DSML-prefixed tool calls ──


def test_dsml_tool_calls():
    """DeepSeek sometimes prefixes XML with <||DSML||> — parser should handle both."""
    body = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "Here is the result.\n\n"
                        "<||DSML||tool_calls>"
                        '<||DSML||invoke name="submit_contribution">'
                        '<||DSML||parameter name="event_name" string="true">INVENTORY MOVEMENT</||DSML||parameter>'
                        '<||DSML||parameter name="qr_code" string="true">2024OSCAR_20260330_22</||DSML||parameter>'
                        "</||DSML||invoke>"
                        "</||DSML||tool_calls>"
                    ),
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
    }
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = _make_client(body)

    resp = p.chat("You are helpful.", [{"role": "user", "content": "submit movement"}])

    assert "||DSML||" not in resp.text
    assert "Here is the result." in resp.text
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["function"]["name"] == "submit_contribution"
    args = json.loads(resp.tool_calls[0]["function"]["arguments"])
    assert args["event_name"] == "INVENTORY MOVEMENT"
    assert args["qr_code"] == "2024OSCAR_20260330_22"


# ── Test 4: Usage populated correctly ──


def test_usage_populated():
    body = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "total_tokens": 75,
            "prompt_cache_hit_tokens": 10,
        },
    }
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = _make_client(body)

    resp = p.chat("You are helpful.", [{"role": "user", "content": "hi"}])

    assert resp.usage.prompt_tokens == 50
    assert resp.usage.completion_tokens == 25
    assert resp.usage.total_tokens == 75
    assert resp.usage.cached_tokens == 10


# ── Test 5: estimate_cost returns non-null float ──


def test_estimate_cost():
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    usage = LLMUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    cost = p.estimate_cost(usage)
    assert cost is not None
    assert cost > 0
    # 1000 * 0.27 / 1M + 500 * 1.10 / 1M = 0.00027 + 0.00055 = 0.00082
    assert abs(cost - 0.00082) < 0.00001


# ── Test 6: HTTPStatusError surfaces as LLMError ──


def test_http_error_surfaces_as_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = httpx.Client(transport=transport, base_url=p.base_url)

    with pytest.raises(LLMError) as exc_info:
        p.chat("You are helpful.", [{"role": "user", "content": "hi"}])
    assert "429" in str(exc_info.value)
    assert "rate limited" in str(exc_info.value)


# ── Test 7: Empty content fallback ──


def test_empty_content():
    body = {
        "choices": [
            {"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }
    p = DeepSeekProvider(
        api_key="test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
    )
    p._http = _make_client(body)

    resp = p.chat("You are helpful.", [{"role": "user", "content": "hi"}])

    assert resp.text == ""
    assert resp.usage.total_tokens == 10
