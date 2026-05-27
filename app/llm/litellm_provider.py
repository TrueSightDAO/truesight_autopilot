"""LiteLLM provider — unified interface for DeepSeek, Claude, BigModel, etc.

Replaces the homegrown HTTP transport and regex-based XML/DSML parsing
with litellm's battle-tested provider abstraction.  Tool calls come back as
standard OpenAI tool_calls arrays; no XML leak, no DSML shenanigans.

Model naming follows litellm's convention:
    deepseek/deepseek-chat   →  DeepSeek V3
    deepseek/deepseek-reasoner  →  DeepSeek R1
    anthropic/claude-sonnet-4-20250514  →  Claude
    openai/gpt-4o            →  OpenAI
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import litellm

from ..config import settings as _settings
from .base import LLMError, LLMProvider, LLMResponse, LLMUsage

logger = logging.getLogger("autopilot.llm.litellm")

LITELLM_MODEL = os.getenv("LITELLM_MODEL", "deepseek/deepseek-chat")

PRICING: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-chat": (0.27, 1.10),
    "deepseek/deepseek-reasoner": (0.55, 2.19),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "anthropic/claude-sonnet-4-20250514": (3.00, 15.00),
    "anthropic/claude-3-5-haiku-20241022": (0.80, 4.00),
}


class LiteLLMProvider(LLMProvider):
    name = "litellm"
    default_model = LITELLM_MODEL
    pricing = PRICING

    def __init__(self) -> None:
        api_key = _settings.deepseek_api_key
        if api_key:
            os.environ.setdefault("DEEPSEEK_API_KEY", api_key)

    def supports_tools(self) -> bool:
        return True

    def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        caller: str = "chat",
        session_id: str | None = None,
        turn: int | None = None,
        round_num: int = 1,
    ) -> LLMResponse:
        t0 = time.time()
        model = self.default_model

        litellm_messages = [{"role": "system", "content": system_prompt}, *messages]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": litellm_messages,
            "temperature": temperature if temperature is not None else _settings.deepseek_temperature,
            "max_tokens": max_tokens or _settings.deepseek_max_tokens,
            "timeout": 120,
            "num_retries": 2,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            resp = litellm.completion(**kwargs)
        except litellm.exceptions.APIConnectionError as exc:
            raise LLMError(f"{model}: connection error — {exc}") from exc
        except litellm.exceptions.APIError as exc:
            raise LLMError(f"{model}: API error — {exc}") from exc
        except litellm.exceptions.Timeout as exc:
            raise LLMError(f"{model}: timed out — {exc}") from exc
        except Exception as exc:
            raise LLMError(f"{model}: {exc}") from exc

        choice = resp.choices[0]
        message = choice.message
        finish = choice.finish_reason or "stop"

        tool_calls: list[dict[str, Any]] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        usage = LLMUsage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
        )

        latency_ms = int((time.time() - t0) * 1000)
        had_tools = bool(tool_calls)

        response = LLMResponse(
            text=message.content or "",
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else {},
            est_usd=self.estimate_cost(usage),
            model=model,
            provider=self.name,
        )

        from .usage_log import log_usage as _log

        _log(
            provider=self.name,
            model=model,
            usage=usage,
            caller=caller,
            session_id=session_id,
            turn=turn,
            round_num=round_num,
            latency_ms=latency_ms,
            had_tool_calls=had_tools,
            finish_reason=finish,
        )

        return response
