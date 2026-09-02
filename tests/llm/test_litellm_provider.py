"""Tests for LiteLLMProvider — Claude (ANTHROPIC_API_KEY) env bridging + pricing.

The bridge matters because pydantic-settings only populates DECLARED fields;
litellm looks up ANTHROPIC_API_KEY in os.environ, so the provider must bridge
the settings value into the environment exactly like DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import os

import pytest

from app.llm import litellm_provider
from app.llm.litellm_provider import LiteLLMProvider


@pytest.fixture
def _clean_env(monkeypatch):
    """Remove ANTHROPIC_API_KEY / DEEPSEEK_API_KEY before each test (isolate bridge)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # point settings fields at known values via monkeypatch
    return monkeypatch


def test_anthropic_key_is_bridged_into_os_environ(_clean_env):
    _clean_env.setattr(
        litellm_provider._settings, "anthropic_api_key", "sk-ant-test-123"
    )
    _clean_env.setattr(litellm_provider._settings, "deepseek_api_key", "sk-ds-test")
    LiteLLMProvider()
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-123"


def test_bridge_uses_setdefault_not_overwrite(_clean_env):
    """A pre-set env key must win (setdefault semantics, mirrors DEEPSEEK)."""
    _clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-existing")
    _clean_env.setattr(
        litellm_provider._settings, "anthropic_api_key", "sk-ant-from-settings"
    )
    LiteLLMProvider()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-existing"


def test_no_anthropic_key_leaves_env_untouched(_clean_env):
    _clean_env.setattr(litellm_provider._settings, "anthropic_api_key", "")
    _clean_env.setattr(litellm_provider._settings, "deepseek_api_key", "")
    LiteLLMProvider()
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_deepseek_bridge_still_works(_clean_env):
    _clean_env.setattr(litellm_provider._settings, "deepseek_api_key", "sk-ds-bridge")
    _clean_env.setattr(litellm_provider._settings, "anthropic_api_key", "")
    LiteLLMProvider()
    assert os.environ.get("DEEPSEEK_API_KEY") == "sk-ds-bridge"


def test_pricing_includes_claude_models():
    for model in (
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
    ):
        assert model in litellm_provider.PRICING
        inp, out = litellm_provider.PRICING[model]
        assert inp > 0 and out > 0


def test_default_model_is_deepseek_v4_flash():
    assert LiteLLMProvider.default_model.endswith("deepseek-v4-flash")
