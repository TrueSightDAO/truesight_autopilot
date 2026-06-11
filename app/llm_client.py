"""LLM client: shim delegating to DeepSeekProvider (app/llm/ package).

Backwards-compatible — same constructor, same return shapes, same methods.
XML/DSML tool-call parsing now lives in DeepSeekProvider, not here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import settings
from .llm.base import LLMError
from .llm.registry import get_provider

logger = logging.getLogger("autopilot.llm")


class LLMClient:
    """Thin shim around the LLM provider abstraction.

    Delegates to app/llm/ package. Same API as before Phase 2.
    """

    def __init__(self, provider_name: str | None = None) -> None:
        self._provider = get_provider(provider_name or settings.llm_provider)
        self.model = self._provider.default_model
        self.max_tokens = settings.deepseek_max_tokens
        self.temperature = settings.deepseek_temperature

    def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Send a chat completion. Returns dict matching the pre-Phase-2 shape.

        Extra kwargs forwarded to the provider (caller, session_id, turn, round_num).
        """
        try:
            resp = self._provider.chat(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                temperature=temperature if temperature is not None else self.temperature,
                max_tokens=max_tokens or self.max_tokens,
                **extra,
            )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"LLM call failed: {exc}") from exc

        # Convert LLMResponse back to the legacy dict shape
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": resp.text,
                        "tool_calls": resp.tool_calls,
                    },
                    "finish_reason": resp.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            },
            "_provider_resp": resp,  # for future consumers that want the full object
        }

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """Simple completion (used by diagnosis engine)."""
        resp = self.chat(system, messages, temperature=temperature, max_tokens=max_tokens)
        return self.extract_text(resp)

    def extract_text(self, completion: dict[str, Any]) -> str:
        choices = completion.get("choices", [])
        if not choices:
            logger.warning("LLM response has no choices: %s", json.dumps(completion)[:500])
            return "(no response)"
        message = choices[0].get("message", {})
        content = message.get("content")
        if not content:
            logger.warning(
                "LLM response has empty content. Finish reason: %s. Message keys: %s",
                choices[0].get("finish_reason"),
                list(message.keys()),
            )
        return content or "(empty response)"

    def extract_tool_calls(self, completion: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract tool calls from the completion.

        XML/DSML fallback is handled by DeepSeekProvider — tool_calls are always
        in the standard array by the time they reach this method.
        """
        choices = completion.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})
        return message.get("tool_calls", []) or []

    def diagnose_github_failure(
        self,
        repo: str,
        workflow_name: str,
        run_url: str,
        log_snippet: str,
    ) -> dict[str, str]:
        """Returns {"root_cause": ..., "proposed_fix": ..., "files_to_edit": ...}"""
        system = (
            "You are an SRE engineer. Analyze the GitHub Actions failure log and propose "
            "a concise fix. Respond in JSON with keys: root_cause, proposed_fix, files_to_edit."
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Repo: {repo}\n"
                    f"Workflow: {workflow_name}\n"
                    f"Run URL: {run_url}\n"
                    f"Log snippet:\n{log_snippet}\n\n"
                    "What failed and how do we fix it?"
                ),
            }
        ]
        try:
            raw = self.complete(system, messages, temperature=0.2, max_tokens=2048)
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            return json.loads(raw)
        except Exception as e:
            logger.error("LLM diagnosis failed: %s", e)
            return {
                "root_cause": "Unable to diagnose — LLM error",
                "proposed_fix": raw[:500] if "raw" in dir() else "N/A",
                "files_to_edit": "",
            }


# Default tool schemas for governor chat
def get_tool_schemas() -> list[dict[str, Any]]:
    """Tool schemas for the LLM tool-call surface.

    Auto-discovered from the capability manifest — each tool module under
    ``app/tools/`` exports a ``TOOL_SPEC`` or ``TOOL_SPECS``. See
    ``app/tool_registry.py`` + ``app/tools/README.md``.
    """
    from .tool_registry import discover_tools

    return [spec.to_openai_schema() for spec in discover_tools()]
