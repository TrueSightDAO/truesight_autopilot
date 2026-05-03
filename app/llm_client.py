"""Swappable LLM client: DeepSeek-V3 primary, Claude fallback."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("autopilot.llm")


class LLMClient:
    def __init__(self, provider: str | None = None):
        self.provider = (provider or "deepseek").lower()
        self._http = httpx.Client(timeout=120.0)

    def complete(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        if self.provider == "deepseek":
            return self._deepseek(system, messages, temperature, max_tokens)
        elif self.provider == "anthropic":
            return self._anthropic(system, messages, temperature, max_tokens)
        else:
            raise ValueError(f"Unknown LLM provider: {self.provider}")

    def _deepseek(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")

        payload = {
            "model": settings.deepseek_model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        r = self._http.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

    def _anthropic(
        self,
        system: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        payload = {
            "model": settings.anthropic_model,
            "system": system,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        r = self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]

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
            # Try to extract JSON from markdown code block if present
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
