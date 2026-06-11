"""BigModel (ZhipuAI / GLM) provider.

OpenAI-compatible endpoint at https://open.bigmodel.cn/api/paas/v4.
GLM-4.5+ models support native tool/function calling.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings as _settings
from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger("autopilot.llm.bigmodel")


class BigModelProvider(OpenAICompatibleProvider):
    name = "bigmodel"
    pricing = {"glm-4.5": (0.014, 0.014)}  # USD per million tokens (estimated)

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url or _settings.bigmodel_base_url
        self.api_key = api_key or _settings.bigmodel_api_key
        self.default_model = default_model or _settings.bigmodel_model
        super().__init__(
            base_url=self.base_url,
            api_key=self.api_key,
            default_model=self.default_model,
            timeout=timeout,
        )

    def _normalize_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """GLM-4 returns standard OpenAI tool_calls — no quirks needed."""
        return message.get("tool_calls", []) or []
