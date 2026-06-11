"""DeepSeek provider with XML/DSML tool-call fallback.

DeepSeek-chat sometimes emits tool calls as XML in the content field
instead of in the standard tool_calls array. This provider handles both
variants transparently.
"""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

from ..config import settings as _settings
from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger("autopilot.llm.deepseek")


class DeepSeekProvider(OpenAICompatibleProvider):
    name = "deepseek"
    pricing = {"deepseek-chat": (0.27, 1.10)}  # input, output USD per million tokens

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url or _settings.deepseek_base_url
        self.api_key = api_key or _settings.deepseek_api_key
        self.default_model = default_model or _settings.deepseek_model
        self._max_tokens = _settings.deepseek_max_tokens
        self._temperature = _settings.deepseek_temperature
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
    ):
        return super().chat(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            temperature=temperature if temperature is not None else self._temperature,
            max_tokens=max_tokens or self._max_tokens,
        )

    # ── Ported verbatim from llm_client.py (battle-tested against real DeepSeek responses) ──

    def _normalize_tool_calls(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool_calls with XML/DSML fallback."""
        std_calls = message.get("tool_calls", []) or []
        if std_calls:
            return std_calls

        content = message.get("content", "") or ""
        if not content:
            return []

        parsed = self._parse_xml_tool_calls(content)
        if parsed:
            logger.info(
                "XML tool-call fallback: parsed %d calls from content", len(parsed)
            )
            # Rewrite message in-place so standard path works downstream
            message["tool_calls"] = parsed
            message["content"] = self._strip_provider_artifacts(content)
            return parsed
        return []

    def _parse_xml_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """Parse DeepSeek XML/DSML tool-call syntax from content.

        Handles two variants DeepSeek emits:
        1. Standard XML: <function_calls><invoke name="..."><parameter name="...">value</parameter></invoke></function_calls>
        2. DSML-prefixed: <||DSML||tool_calls><||DSML||invoke name="..."><||DSML||parameter name="..." string="true">value</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>
        """
        calls: list[dict[str, Any]] = []

        # Normalize DSML prefixes to standard XML tags for unified parsing
        normalized = text
        normalized = _re.sub(r"<\|\|DSML\|\|", "<", normalized)
        normalized = _re.sub(r"</\|\|DSML\|\|", "</", normalized)

        # Pattern: <invoke name="func_name">...params...</invoke>
        invoke_pattern = _re.compile(
            r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>',
            _re.DOTALL,
        )
        # Pattern: <parameter name="name">value</parameter>
        param_pattern = _re.compile(
            r'<parameter\s+name="([^"]+)"[^>]*>\s*(.*?)\s*</parameter>',
            _re.DOTALL,
        )

        body = text  # fallback
        wrapper_match = _re.search(
            r"<(?:function_calls|tool_calls)>\s*(.*?)\s*</(?:function_calls|tool_calls)>",
            normalized,
            _re.DOTALL,
        )
        if wrapper_match:
            body = wrapper_match.group(1)
        else:
            # Use normalized text for invoke matching if no wrapper found
            body = normalized

        for idx, match in enumerate(invoke_pattern.finditer(body)):
            func_name = match.group(1)
            params_body = match.group(2)
            args: dict[str, object] = {}
            for pm in param_pattern.finditer(params_body):
                key = pm.group(1)
                val = pm.group(2).strip()
                if val:
                    args[key] = val
            calls.append(
                {
                    "id": f"call_xml_{idx:02d}",
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": json.dumps(args),
                    },
                }
            )
        return calls

    def _strip_provider_artifacts(self, content: str) -> str:
        """Remove <function_calls>...</function_calls> or DSML equivalent from text."""
        text = content
        # Strip both standard XML and DSML-prefixed tool call wrappers
        text = _re.sub(
            r"<(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>.*?</(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>",
            "",
            text,
            flags=_re.DOTALL,
        )
        # Strip DSML invoke blocks not wrapped in a parent tag
        text = _re.sub(
            r'<\|\|DSML\|\|invoke\s+name="[^"]+"\s*>.*?</\|\|DSML\|\|invoke>',
            "",
            text,
            flags=_re.DOTALL,
        )
        # Strip standalone (non-DSML) invoke blocks — DeepSeek sometimes emits
        # <invoke name="func"><parameter ...>value</parameter></invoke> without
        # any wrapper tag, leaking raw XML into the chat response.
        text = _re.sub(
            r'<invoke\s+name="[^"]+"\s*>.*?</invoke>',
            "",
            text,
            flags=_re.DOTALL,
        )
        return text.strip()
