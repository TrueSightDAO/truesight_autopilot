"""BigModel.cn / Zhipu AI provider.

OpenAI-compatible. Uses GLM-4 series models. Provided by Elizabeth Wong
(DAO contributor) with $1,000 USD credit. Each call's est_usd is tracked
and rolled up into daily [CONTRIBUTION EVENT] submissions via
scripts/rollup_llm_contributions.py.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger("autopilot.llm.bigmodel")

# Contributor credited for BigModel.cn token costs
_BIGMODEL_CONTRIBUTOR = os.getenv("BIGMODEL_CONTRIBUTOR", "Elizabeth Wong")


class BigModelProvider(OpenAICompatibleProvider):
    name = "bigmodel"
    # Pricing TBD — populate once we sample a few calls from Liz's account tier.
    # Format: {model: (input_per_M, output_per_M)} in USD per million tokens.
    pricing: dict[str, tuple[float, float]] = {}

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 120.0,
        contributor_name: str | None = None,
    ) -> None:
        api_key = api_key or os.getenv("BIGMODEL_CN_API", "")
        self.base_url = base_url or "https://open.bigmodel.cn/api/paas/v4"
        self.api_key = api_key
        self.default_model = default_model or "glm-4.6"
        self.contributor_name = contributor_name or _BIGMODEL_CONTRIBUTOR
        super().__init__(
            base_url=self.base_url,
            api_key=self.api_key,
            default_model=self.default_model,
            timeout=timeout,
        )

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
    ):
        return super().chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            temperature=temperature if temperature is not None else 0.3,
            max_tokens=max_tokens or 16384,
            caller=caller,
            session_id=session_id,
            turn=turn,
            round_num=round_num,
        )
