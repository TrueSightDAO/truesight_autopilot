"""OpenAI-compatible provider base class.

Handles HTTP transport, error handling, and response normalisation.
Subclasses override _normalize_tool_calls and _strip_provider_artifacts
for provider-specific quirks (e.g. DeepSeek's XML tool calls).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import LLMError, LLMProvider, LLMResponse, LLMUsage

logger = logging.getLogger("autopilot.llm")


class OpenAICompatibleProvider(LLMProvider):
    """Base for providers with OpenAI-compatible /chat/completions endpoints.

    Subclasses should set:
      - name, default_model, base_url, api_key (class or instance attrs)
      - pricing dict for cost estimation
    """

    base_url: str = ""
    api_key: str = ""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if base_url is not None:
            self.base_url = base_url
        if api_key is not None:
            self.api_key = api_key
        if default_model is not None:
            self.default_model = default_model
        if not self.api_key:
            raise LLMError(f"{self.name}: API key is not set")
        self._http = httpx.Client(timeout=timeout)

    def supports_tools(self) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _normalize_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool_calls from a message dict.

        Default: return the standard tool_calls array.
        Subclasses override for provider-specific quirks.
        """
        return message.get("tool_calls", []) or []

    def _strip_provider_artifacts(self, content: str) -> str:
        """Strip provider-specific artifacts from content text.

        Default: identity. Subclasses override to remove XML/DSML etc.
        """
        return content

    def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a chat completion and return a normalised LLMResponse."""
        payload: dict[str, Any] = {
            "model": self.default_model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_tokens": max_tokens or 16384,
            "temperature": temperature if temperature is not None else 0.3,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        base = self.base_url.rstrip("/")

        try:
            resp = self._http.post(
                f"{base}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as exc:
            err_body = exc.response.text[:1000]
            logger.error("%s API error %s: %s", self.name, exc.response.status_code, err_body)
            raise LLMError(
                f"{self.name} API error {exc.response.status_code}: {err_body}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("%s request failed: %s", self.name, exc)
            raise LLMError(f"{self.name} request failed: {exc}") from exc

        choices = body.get("choices", [])
        if not choices:
            logger.warning("%s response has no choices: %s", self.name, str(body)[:500])
            return LLMResponse(
                text="(no response)",
                finish_reason="error",
                raw=body,
                model=self.default_model,
                provider=self.name,
            )

        message = choices[0].get("message", {})
        finish_reason = choices[0].get("finish_reason", "stop")

        # Normalize tool calls (provider-specific hooks)
        tool_calls = self._normalize_tool_calls(message)

        # Strip provider artifacts from content
        raw_content = message.get("content", "") or ""
        content = self._strip_provider_artifacts(raw_content)

        # Extract usage
        usage_raw = body.get("usage", {})
        usage = LLMUsage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
            cached_tokens=usage_raw.get("prompt_cache_hit_tokens", 0),
        )

        return LLMResponse(
            text=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            raw=body,
            est_usd=self.estimate_cost(usage),
            model=self.default_model,
            provider=self.name,
        )
