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
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": resp.text,
                    "tool_calls": resp.tool_calls,
                },
                "finish_reason": resp.finish_reason,
            }],
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
            logger.warning("LLM response has empty content. Finish reason: %s. Message keys: %s",
                           choices[0].get("finish_reason"),
                           list(message.keys()))
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
    ALLOWED_CHAT_REPOS = ", ".join([
        "dapp_beta", "dapp_prod", "tokenomics", "truesight_me", "truesight_me_prod",
        "agroverse_shop", "agroverse_shop_prod", "dao_client",
        "market_research", "sentiment_importer", "truesight_autopilot",
        ".github", "agentic_ai_context", "agroverse-inventory", "dao_protocol",
    ])
    return [
        {
            "type": "function",
            "function": {
                "name": "list_org_repos",
                "description": "List all repositories in the TrueSightDAO GitHub organization. Use this to discover what repos exist.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_context_file",
                "description": "Read a file from the agentic_ai_context repository.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Relative path inside agentic_ai_context, e.g. 'WORKSPACE_CONTEXT.md'"}},
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
                        "repo": {"type": "string", "description": f"GitHub repo name under TrueSightDAO. Allowed: {ALLOWED_CHAT_REPOS}"},
                        "path": {"type": "string", "description": "File path in the repo."},
                        "ref": {"type": "string", "description": "Branch or commit. Default: main", "default": "main"},
                    },
                    "required": ["repo", "path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_contribution",
                "description": "Submit a signed [CONTRIBUTION EVENT] or other event to Edgar (the DAO API).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_name": {"type": "string", "description": "Event name, e.g. 'CONTRIBUTION EVENT', 'INVENTORY MOVEMENT'."},
                        "attributes": {"type": "object", "description": "Key-value pairs describing the event."},
                    },
                    "required": ["event_name", "attributes"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_fix_pr",
                "description": "Run a full agentic loop to diagnose and fix an issue in any TrueSightDAO repo. Opens a DRAFT PR.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": f"Repo name under TrueSightDAO. Allowed: {ALLOWED_CHAT_REPOS}"},
                        "issue_description": {"type": "string", "description": "Description of the issue to fix."},
                    },
                    "required": ["repo", "issue_description"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scan_qr_from_file",
                "description": "Scan a single image file for QR codes and return the decoded values.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string", "description": "Full path to the image file."}},
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scan_qr_batch",
                "description": "Batch-scan multiple image files for QR codes.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_paths": {"type": "array", "items": {"type": "string"}, "description": "List of full paths to image files."}},
                    "required": ["file_paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_qr_code",
                "description": "Look up a single Agroverse QR code in the DAO ledger (read-only).",
                "parameters": {
                    "type": "object",
                    "properties": {"qr_code": {"type": "string", "description": "The QR code identifier."}},
                    "required": ["qr_code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_qr_batch",
                "description": "Look up multiple QR codes at once.",
                "parameters": {
                    "type": "object",
                    "properties": {"qr_codes": {"type": "array", "items": {"type": "string"}, "description": "List of QR code identifiers."}},
                    "required": ["qr_codes"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "register_identity",
                "description": "Register a new DAO identity by generating an RSA-2048 keypair and submitting to Edgar.",
                "parameters": {
                    "type": "object",
                    "properties": {"email": {"type": "string", "description": "The email address to register."}},
                    "required": ["email"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_matching_qr_codes",
                "description": "Search previously looked-up QR codes by prefix.",
                "parameters": {
                    "type": "object",
                    "properties": {"prefix": {"type": "string", "description": "QR code prefix to match."}},
                    "required": ["prefix"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "upload_file_to_github",
                "description": "Create or update a file in a TrueSightDAO GitHub repo. Content is auto-encoded (pass plain text).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repo name under TrueSightDAO."},
                        "path": {"type": "string", "description": "Path inside the repo, e.g. 'reports/market_analysis.md'."},
                        "content": {"type": "string", "description": "The file content as plain text (base64-encoding is handled automatically)."},
                        "message": {"type": "string", "description": "Short one-line commit message (max 72 chars), e.g. 'add market analysis report'."},
                        "branch": {"type": "string", "description": "Branch name. Default: main", "default": "main"},
                    },
                    "required": ["repo", "path", "content", "message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files in a local directory on the server.",
                "parameters": {
                    "type": "object",
                    "properties": {"dir_path": {"type": "string", "description": "Full path to the directory."}},
                    "required": ["dir_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_local_file",
                "description": "Read a local text file from the server filesystem.",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string", "description": "Full path to the file."}},
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "merge_pr",
                "description": "Merge a pull request. Only use when a governor explicitly tells you to merge.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repo name under TrueSightDAO."},
                        "pr_number": {"type": "integer", "description": "The pull request number to merge."},
                        "merge_method": {"type": "string", "description": "squash (default), merge, or rebase.", "enum": ["squash", "merge", "rebase"], "default": "squash"},
                    },
                    "required": ["repo", "pr_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "deploy_autopilot",
                "description": "Deploy the latest version of truesight_autopilot to EC2 via SSH.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_prs",
                "description": "List recent pull requests on a TrueSightDAO repo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "Repo name under TrueSightDAO."},
                        "state": {"type": "string", "description": "open, closed, or all.", "enum": ["open", "closed", "all"], "default": "all"},
                        "limit": {"type": "integer", "description": "Max PRs to return.", "default": 20},
                    },
                    "required": ["repo"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_dao_submission",
                "description": "Submit a [CONTRIBUTION EVENT] to Edgar for DAO contribution tracking.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short one-line title."},
                        "body": {"type": "string", "description": "Multi-line description."},
                        "pr_urls": {"type": "array", "items": {"type": "string"}, "description": "PR URLs as evidence."},
                        "contributors": {"type": "string", "description": "Display name."},
                        "amount": {"type": "string", "description": "Minutes or dollar amount.", "default": "0"},
                        "tdg_issued": {"type": "string", "description": "TDG to issue.", "default": "0"},
                    },
                    "required": ["title", "body", "pr_urls"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_oracle_logs",
                "description": "Read oracle draw logs from TrueSightDAO/oracle_logs.",
                "parameters": {
                    "type": "object",
                    "properties": {"date": {"type": "string", "description": "YYYY-MM-DD date, 'latest', or omit to list draws.", "default": "latest"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the live, public web (via Tavily) for current information not in the DAO context or repos — news, docs, prices, people, external facts. Returns ranked results with snippets and an optional synthesized answer. Use web_extract afterward to read a specific result in full.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "max_results": {"type": "integer", "description": "Number of results (1-10).", "default": 5},
                        "search_depth": {"type": "string", "description": "'basic' (fast) or 'advanced' (deeper).", "enum": ["basic", "advanced"], "default": "basic"},
                        "include_answer": {"type": "boolean", "description": "Include a synthesized answer.", "default": True},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_extract",
                "description": "Fetch and return the cleaned full-text content of one or more specific web page URLs (via Tavily). Use after web_search to read a promising result in depth, or when the user gives you a URL to read.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "urls": {"type": "array", "items": {"type": "string"}, "description": "List of page URLs to read (max 10)."},
                    },
                    "required": ["urls"],
                },
            },
        },
    ]
