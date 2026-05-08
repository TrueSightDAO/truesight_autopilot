"""LLM provider abstraction layer (Phase 1 — additive, no callers yet).

Re-exports for downstream consumers:
  get_provider — factory that returns a cached LLMProvider instance
  LLMProvider  — abstract base class
  LLMResponse  — normalised response dataclass
  LLMUsage     — token usage dataclass
  LLMError     — provider error exception
"""

from .base import LLMError, LLMProvider, LLMResponse, LLMUsage
from .registry import get_provider

__all__ = [
    "get_provider",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "LLMError",
]
