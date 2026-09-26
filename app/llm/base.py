"""LLM provider abstract base class and shared dataclasses.

Matches the field set in truesight_autopilot_transcript/SCHEMA.md §3 (usage.jsonl).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """Token usage for a single LLM call.

    Matches SCHEMA.md §3 fields: prompt_tokens, completion_tokens, total_tokens,
    cached_tokens.  est_usd is computed by the provider, not the API.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class LLMResponse:
    """Normalised response from any LLM provider.

    Fields match what consumers (chat loop, fix agent, email poller) need:
    - text: assistant content string (may be empty if only tool_calls)
    - tool_calls: list of standard OpenAI-format tool calls (empty if none)
    - usage: token counts
    - finish_reason: 'stop' | 'tool_calls' | 'length' | 'content_filter' | 'error'
    - raw: the raw provider response dict (for debugging / logging)
    - est_usd: estimated cost computed from usage + pricing dict
    - model: model name used
    - provider: provider key
    """

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)
    est_usd: float | None = None
    model: str = ""
    provider: str = ""


class LLMError(Exception):
    """Raised when an LLM provider call fails."""

    pass


class LLMProvider(ABC):
    """Abstract base for LLM providers.

    Subclasses must implement:
      - name: str (provider key, e.g. 'deepseek', 'bigmodel')
      - default_model: str
      - chat() -> LLMResponse

    Subclasses may override:
      - estimate_cost(usage) -> float|None  (per-class pricing dict)
      - pricing dict: {model: (in_per_M, out_per_M)}
    """

    name: str
    default_model: str
    pricing: dict[str, tuple[float, float]] = {}

    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a chat completion and return a normalised LLMResponse."""
        ...

    @abstractmethod
    def supports_tools(self) -> bool:
        """Whether this provider supports tool/function calling."""
        ...

    def estimate_cost(self, usage: LLMUsage, model: str | None = None) -> float | None:
        """Estimate USD cost from token usage and pricing dict.

        pricing dict format: {model: (input_per_M, output_per_M)} in USD per million tokens.
        Returns None if pricing is unknown for this model.
        """
        mdl = model or self.default_model
        rates = self.pricing.get(mdl)
        if rates is None:
            return None
        in_rate, out_rate = rates
        prompt_cost = usage.prompt_tokens * in_rate / 1_000_000
        completion_cost = usage.completion_tokens * out_rate / 1_000_000
        return round(prompt_cost + completion_cost, 6)
