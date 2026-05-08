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
            err_body = exc.response.text[:1000]
            logger.error("DeepSeek API error %s: %s", exc.response.status_code, err_body)
            raise LLMError(f"DeepSeek API error {exc.response.status_code}: {err_body}") from exc
        except httpx.RequestError as exc:
            logger.error("DeepSeek request failed: %s", exc)
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
        """Extract tool calls from completion, with fallback for DeepSeek's XML syntax.

        DeepSeek-chat sometimes emits tool calls as XML in the content field
        instead of in the standard OpenAI `tool_calls` array. This fallback
        detects `<function_calls>` / `<invoke>` XML and converts to proper format.
        """
        choices = completion.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})

        # Standard OpenAI format
        std_calls = message.get("tool_calls", []) or []
        if std_calls:
            return std_calls

        # Fallback: parse DeepSeek XML tool-call syntax from content
        content = message.get("content", "") or ""
        if not content:
            return []

        parsed = self._parse_xml_tool_calls(content)
        if parsed:
            logger.info("XML tool-call fallback: parsed %d calls from content", len(parsed))
            # Rewrite completion in-place so standard path works
            choices[0]["message"]["tool_calls"] = parsed
            # Strip the XML from content so it doesn't leak to the user
            cleaned = self._strip_xml_from_content(content)
            choices[0]["message"]["content"] = cleaned
            return parsed
        return []

    def _parse_xml_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """Parse DeepSeek XML/DSML tool-call syntax from content.

        Handles two variants DeepSeek emits:
        1. Standard XML: <function_calls><invoke name="..."><parameter name="...">value</parameter></invoke></function_calls>
        2. DSML-prefixed: <||DSML||tool_calls><||DSML||invoke name="..."><||DSML||parameter name="..." string="true">value</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>
        """
        import re as _re
        calls: list[dict[str, Any]] = []

        # Normalize DSML prefixes to standard XML tags for unified parsing
        normalized = text
        # Replace ||DSML|| prefixed tags: <||DSML||tag -> <tag, </||DSML||tag -> </tag
        normalized = _re.sub(r'<\|\|DSML\|\|', '<', normalized)
        normalized = _re.sub(r'</\|\|DSML\|\|', '</', normalized)

        # Pattern: <invoke name="func_name">...params...</invoke>
        invoke_pattern = _re.compile(
            r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>',
            _re.DOTALL,
        )
        # Pattern: <parameter name="name" string="true">value</parameter> or <parameter name="name">value</parameter>
        param_pattern = _re.compile(
            r'<parameter\s+name="([^"]+)"[^>]*>\s*(.*?)\s*</parameter>',
            _re.DOTALL,
        )

        # Also match <function_calls> or <tool_calls> wrapper
        body = text
        wrapper_match = _re.search(
            r'<(?:function_calls|tool_calls|(?:\|\|DSML\|\|)?tool_calls)>\s*(.*?)\s*</(?:function_calls|tool_calls|(?:\|\|DSML\|\|)?tool_calls)>',
            text,
            _re.DOTALL,
        )
        if wrapper_match:
            body = wrapper_match.group(1)

        for idx, match in enumerate(invoke_pattern.finditer(body)):
            func_name = match.group(1)
            params_body = match.group(2)
            args: dict[str, object] = {}
            for pm in param_pattern.finditer(params_body):
                key = pm.group(1)
                val = pm.group(2).strip()
                # Remove string="true" boolean attribute values if present (already handled by regex)
                if val:
                    args[key] = val
            calls.append({
                "id": f"call_xml_{idx:02d}",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps(args),
                },
            })
        return calls

    @staticmethod
    def _strip_xml_from_content(text: str) -> str:
        """Remove <function_calls>...</function_calls> or DSML equivalent from text."""
        import re as _re
        # Strip both standard XML and DSML-prefixed tool call wrappers
        text = _re.sub(
            r'<(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>.*?</(?:function_calls|(?:\|\|DSML\|\|)?tool_calls)>',
            '',
            text,
            flags=_re.DOTALL,
        )
        # Also strip any remaining DSML invoke blocks not wrapped in a parent tag
        text = _re.sub(
            r'<\|\|DSML\|\|invoke\s+name="[^"]+"\s*>.*?</\|\|DSML\|\|invoke>',
            '',
            text,
            flags=_re.DOTALL,
        )
        return text.strip()

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
        ".github", "agentic_ai_context", "agroverse-inventory",
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
                "name": "submit_contribution",
                "description": "Submit a signed [CONTRIBUTION EVENT] or other event to Edgar (the DAO API). Use this to log transactions, cacao bags received, sales, contributions, or any DAO record.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_name": {
                            "type": "string",
                            "description": "Event name in square-bracket convention, e.g. 'CONTRIBUTION EVENT', 'BAG RECEIPT', 'SALE'",
                        },
                        "attributes": {
                            "type": "object",
                            "description": "Key-value pairs describing the event. Include Type, Amount, Description, Contributors, etc.",
                        },
                    },
                    "required": ["event_name", "attributes"],
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
                "name": "scan_qr_from_file",
                "description": "Scan a single image file for QR codes and return the decoded values. Use this when the user uploads photos of QR codes (e.g. from cacao bags).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Full path to the image file on disk (e.g. /tmp/autopilot_uploads/abc123.jpg).",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scan_qr_batch",
                "description": "Batch-scan multiple image files for QR codes. Returns a summary of all QR codes found across all images. Use when the user uploads many photos at once.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of full paths to image files.",
                        },
                    },
                    "required": ["file_paths"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_qr_code",
                "description": "Look up a single Agroverse QR code in the DAO ledger (read-only). Returns the QR code's currency, ledger shortcut, status, owner, manager, and shipping info. Use this to check what a QR code represents before recording a transaction.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "qr_code": {
                            "type": "string",
                            "description": "The QR code identifier (e.g. 2024OSCAR_20260121_12).",
                        },
                    },
                    "required": ["qr_code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_qr_batch",
                "description": "Look up multiple QR codes at once. Returns a summary of found/missing records. Use after scan_qr_batch to resolve all detected codes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "qr_codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of QR code identifiers to look up.",
                        },
                    },
                    "required": ["qr_codes"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "register_identity",
                "description": "Register a new DAO identity by generating an RSA-2048 keypair, signing an [EMAIL REGISTERED EVENT], submitting to Edgar, and saving the keys to .env. Use this to register yourself or a new contributor in Contributors Digital Signatures.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The email address to register as the DAO contributor identity (e.g. admin@truesight.me).",
                        },
                    },
                    "required": ["email"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_matching_qr_codes",
                "description": "Search previously looked-up QR codes by prefix. Use this when you have a partial QR code (e.g. from a blurry photo) and need to find matching full codes. Only returns codes that have been previously cached via lookup_qr_code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prefix": {
                            "type": "string",
                            "description": "QR code prefix to match against cached lookups, e.g. '2024OSCAR_20260330_' or 'LA_CC_'.",
                        },
                    },
                    "required": ["prefix"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files in a local directory on the server. Returns file names, sizes, and types. Use this to discover files before scanning QR codes or reading them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_path": {
                            "type": "string",
                            "description": "Full path to the directory to list, e.g. '/Users/garyjob/Downloads/'.",
                        },
                    },
                    "required": ["dir_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_local_file",
                "description": "Read a local text file from the server filesystem. Use this to read .env files, google_credentials.json, or any gitignored local file. Returns the file contents as text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Full path to the file on disk, e.g. '/home/ubuntu/truesight_autopilot/.env' or '/Users/garyjob/projects/truesight_autopilot/.env'.",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "upload_file_to_github",
                "description": "Upload a file to a TrueSightDAO GitHub repo via the Contents API. Useful for archiving invoice PDFs, receipts, or other evidence files. Returns the blob URL for use in offchain transaction descriptions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "Repo name under TrueSightDAO, e.g. '.github' for the assets repo.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Path inside the repo, e.g. 'assets/20260506_amazon_invoice.pdf'.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Base64-encoded file content.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Commit message for the upload.",
                        },
                        "branch": {
                            "type": "string",
                            "description": "Branch name. Default: main",
                            "default": "main",
                        },
                    },
                    "required": ["repo", "path", "content", "message"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "merge_pr",
                "description": "Merge a pull request. Only use this when a governor explicitly tells you to merge (e.g. 'merge it', 'merge the PR', 'go ahead and merge'). Never auto-merge on your own. The PR must be from an allowed repo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "Repo name under TrueSightDAO, e.g. 'truesight_autopilot'.",
                        },
                        "pr_number": {
                            "type": "integer",
                            "description": "The pull request number to merge.",
                        },
                        "merge_method": {
                            "type": "string",
                            "description": "Merge method: 'squash' (default), 'merge', or 'rebase'.",
                            "enum": ["squash", "merge", "rebase"],
                            "default": "squash",
                        },
                    },
                    "required": ["repo", "pr_number"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "deploy_autopilot",
                "description": "Deploy the latest version of truesight_autopilot to EC2 via SSH. Pulls latest code, installs deps, restarts systemd, and checks health. Only use this when a governor explicitly tells you to redeploy.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_prs",
                "description": "List recent pull requests on a TrueSightDAO repo. Returns PR numbers, titles, states, and URLs. Use this to find PRs for DAO contribution evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {
                            "type": "string",
                            "description": "Repo name under TrueSightDAO, e.g. 'dapp' or 'truesight_autopilot'.",
                        },
                        "state": {
                            "type": "string",
                            "description": "PR state: 'open', 'closed', or 'all' (default).",
                            "enum": ["open", "closed", "all"],
                            "default": "all",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max PRs to return (default 20).",
                            "default": 20,
                        },
                    },
                    "required": ["repo"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_dao_submission",
                "description": "Submit a [CONTRIBUTION EVENT] to Edgar for DAO contribution tracking. Use for time-based (Time) or USD contributions. Requires at least one TrueSightDAO PR URL as evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short one-line title for the contribution."},
                        "body": {"type": "string", "description": "Multi-line description: what changed, why, evidence."},
                        "pr_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "At least one https://github.com/TrueSightDAO/.../pull/N URL.",
                        },
                        "contributors": {
                            "type": "string",
                            "description": "Display name. Defaults to governor name.",
                        },
                        "amount": {
                            "type": "string",
                            "description": "For Time: total minutes. For USD: dollar amount.",
                            "default": "0",
                        },
                        "tdg_issued": {
                            "type": "string",
                            "description": "TDG to issue. For Time: hours * 100. For USD: same as amount.",
                            "default": "0",
                        },
                    },
                    "required": ["title", "body", "pr_urls"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_oracle_logs",
                "description": "Read oracle draw logs from TrueSightDAO/oracle_logs. Use 'latest' for the most recent draw, or pass YYYY-MM-DD. Omit date to list available draws. Use to surface issues from morning oracle draws for triage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "YYYY-MM-DD date, 'latest', or omit to list draws.",
                            "default": "latest"
                        }
                    }
                }
            }
        },
    ]
