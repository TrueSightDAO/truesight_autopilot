"""Provider registry — maps provider keys to LLMProvider subclasses.

Provider keys MUST stay in sync with PROVIDERS.md §6.
"""
from __future__ import annotations

import logging
from typing import Type

from .base import LLMProvider

logger = logging.getLogger("autopilot.llm.registry")

_PROVIDERS: dict[str, type[LLMProvider]] = {}
_INSTANCES: dict[str, LLMProvider] = {}


def _ensure_providers() -> None:
    """Lazy-import provider classes so registration happens once."""
    if _PROVIDERS:
        return
    from .deepseek import DeepSeekProvider as _D

    _PROVIDERS["deepseek"] = _D


def get_provider(name: str | None = None) -> LLMProvider:
    """Get a cached provider instance by name.

    Defaults to the LLM_PROVIDER setting if no name given.
    """
    _ensure_providers()
    from ..config import settings as _settings

    key = name or getattr(_settings, "llm_provider", "deepseek")
    key = (key or "deepseek").strip().lower()

    if key not in _PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider '{key}'. Known: {list(_PROVIDERS.keys())}"
        )

    if key not in _INSTANCES:
        cls = _PROVIDERS[key]
        _INSTANCES[key] = cls()
        logger.info("Initialized LLM provider: %s (%s)", _INSTANCES[key].name, _INSTANCES[key].default_model)

    return _INSTANCES[key]


def register_provider(key: str, provider_cls: type[LLMProvider]) -> None:
    """Register a provider class under a key. Call before get_provider."""
    _PROVIDERS[key] = provider_cls
