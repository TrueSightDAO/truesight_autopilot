"""LLM client: DeepSeek-V3 for everything (chat, diagnosis, code)."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("autopilot.llm")


class LLMError(Exception):
    pass


class LLMClient:
    """DeepSeek client with OpenAI-compatible API.

    Used for both governor chat (with tools) and autopilot diagnosis.
    """

    def __init__(self) -> None:
        if not settings.deepseek_api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set.")
        self.base_url = settings.deepseek_base_url.rstrip("/")
        self.api_key = settings.deepseek_api_key
        self.model = settings.deepseek_model
        self.max_tokens = settings.deepseek_max_tokens
        self.temperature = settings.deepseek_temperature
        self._http = httpx.Client(timeout=120.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request to DeepSeek."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = self._http.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"DeepSeek API error {exc.response.status_code}: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"DeepSeek request failed: {exc}") from exc

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
            return "(no response)"
        message = choices[0].get("message", {})
        return message.get("content", "") or "(empty response)"

    def extract_tool_calls(self, completion: dict[str, Any]) -> list[dict[str, Any]]:
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
    ALLOWED_CHAT_REPOS = ", ".join([
        "dapp", "tokenomics", "truesight_me", "truesight_me_prod",
        "agroverse_shop", "agroverse_shop_prod", "dao_client",
        "market_research", "sentiment_importer", "truesight_autopilot",
    ])
    return [
        {
            "type": "function",
            "function": {
                "name": "list_org_repos",
                "description": "List all repositories in the TrueSightDAO GitHub organization. Use this to discover what repos exist.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_context_file",
                "description": "Read a file from the agentic_ai_context repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path inside agentic_ai_context, e.g. 'WORKSPACE_CONTEXT.md'",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_repo_file",
                "description": "Read a file from a TrueSightDAO GitHub repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": f"GitHub repo name under TrueSightDAO. Allowed: {ALLOWED_CHAT_REPOS}",
                        },
                        "path": {"type": "string", "description": "File path in the repo."},
                        "ref": {
                            "type": "string",
                            "description": "Branch or commit. Default: main",
                            "default": "main",
                        },
                    },
                    "required": ["repo", "path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_fix_pr",
                "description": "Run a full agentic loop to diagnose and fix an issue in any TrueSightDAO repo. Opens a DRAFT PR that requires human review.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": f"Repo name under TrueSightDAO. Allowed: {ALLOWED_CHAT_REPOS}",
                        },
                        "issue_description": {
                            "type": "string",
                            "description": "Description of the issue to fix — be specific about what needs to change",
                        },
                    },
                    "required": ["repo", "issue_description"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_dao_submission",
                "description": "Compile and submit a [CONTRIBUTION EVENT] to Edgar for AI agent work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short one-line title."},
                        "body": {"type": "string", "description": "Multi-line description with what changed and why."},
                        "pr_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "At least one https://github.com/TrueSightDAO/.../pull/N URL.",
                        },
                        "contributors": {
                            "type": "string",
                            "description": "Display name. Defaults to EMAIL local-part.",
                        },
                        "amount": {"type": "string", "default": "0"},
                        "tdg_issued": {"type": "string", "default": "0"},
                    },
                    "required": ["title", "body", "pr_urls"],
                },
            },
        },
    ]
