"""Autonomous fix agent using DeepSeek tool_calls.

- Safety hooks block dangerous operations (rm -rf, sudo, --force, etc.)
- Branch-based isolation (no local git clone needed)
- Max 10 iterations
- py_compile validation before PR
- Supports all TrueSightDAO repos — see ALLOWED_REPOS in config.py
- Cost: ~$0.002 per fix loop (DeepSeek-V3)
"""
from __future__ import annotations

import json
import logging
import os
import py_compile
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .github_client import GitHubClient
from .llm_client import LLMClient
from .config import settings
from .edgar_logger import EdgarLogger

logger = logging.getLogger("autopilot.fix_agent")

# Hard-block patterns for safety hooks
DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"rm\s+-[rf]", re.IGNORECASE), "rm -rf / rm -r"),
    (re.compile(r"sudo\b", re.IGNORECASE), "sudo"),
    (re.compile(r"--force\b", re.IGNORECASE), "--force"),
    (re.compile(r"curl\s+.*\|\s*(bash|sh)", re.IGNORECASE), "curl | bash"),
    (re.compile(r"wget\s+.*\|\s*(bash|sh)", re.IGNORECASE), "wget | bash"),
    (re.compile(r">\s*/dev/(null|zero|random)", re.IGNORECASE), "dangerous redirect"),
    (re.compile(r"chmod\s+777", re.IGNORECASE), "chmod 777"),
    (re.compile(r"eval\s*\(", re.IGNORECASE), "eval()"),
    (re.compile(r"exec\s*\(", re.IGNORECASE), "exec()"),
]


def _is_dangerous(text: str) -> str | None:
    """Return blocking reason if text matches a dangerous pattern, else None."""
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(text):
            return reason
    return None


class SafetyError(Exception):
    pass


class FixAgent:
    def __init__(self):
        self.github = GitHubClient()
        self.llm = LLMClient()
        self.max_iterations = 10

    def run_simple(self, repo: str, issue_description: str) -> str | None:
        """Run a fix loop from a plain-text issue description.
        The LLM does its own diagnosis as part of the agentic loop.
        
        DRY_RUN does NOT gate the fix agent — it always opens DRAFT PRs,
        never auto-merges, and has safety hooks for dangerous operations.
        DRY_RUN only gates background tasks (email poller, AWS monitor).
        """        branch = f"autopilot/fix-{int(time.time())}"
        if not self.github.create_branch(repo, "main", branch):
            logger.error("Failed to create branch on %s", repo)
            return None

        logger.info("Fix loop started: repo=%s branch=%s", repo, branch)

        repos = ", ".join(settings.allowed_repos)
        system = (
            "You are an autonomous code fixer for TrueSight DAO.\n\n"
            f"Repo: {repo}\n"
            f"Issue: {issue_description}\n\n"
            f"Allowed repos: {repos}\n\n"
            "Rules:\n"
            "1. First, read the relevant files to understand the current code.\n"
            "2. Make MINIMAL changes — fix only what's broken.\n"
            "3. After editing a Python file, run py_compile to validate syntax.\n"
            "4. If you need to create a new file, use create_file.\n"
            "5. If you need to delete a file, use delete_file.\n"
            "6. If you're stuck after 3 attempts, say 'I give up' and stop.\n"
            "7. When done, do not call any more tools.\n"
        )
        tools = self._tool_schemas()
        messages: list[dict[str, Any]] = []

        edits_made = False

        for step in range(self.max_iterations):
            logger.info("Fix iteration %d/%d", step + 1, self.max_iterations)
            try:
                completion = self.llm.chat(system, messages, tools=tools)
            except Exception as e:
                logger.error("LLM chat failed at step %d: %s", step, e)
                break

            assistant = completion.get("choices", [{}])[0].get("message", {})
            content = assistant.get("content", "")
            tool_calls = assistant.get("tool_calls", [])

            if not tool_calls:
                logger.info("Agent finished at step %d", step)
                break

            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": "Error: invalid JSON arguments",
                    })
                    continue

                args_str = json.dumps(func_args)
                danger = _is_dangerous(args_str)
                if danger:
                    logger.warning("Safety hook blocked %s: %s", func_name, danger)
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": f"BLOCKED by safety hook: {danger} detected in arguments",
                    })
                    continue

                try:
                    result = self._execute_tool(repo, branch, func_name, func_args)
                except Exception as e:
                    result = f"Tool error: {e}"
                    logger.error("Tool %s failed: %s", func_name, e)

                messages.append({
                    "role": "tool", "tool_call_id": tc["id"], "content": result,
                })

                if func_name in ("edit_file", "create_file", "delete_file") and "successfully" in result:
                    edits_made = True

        if not edits_made:
            logger.info("No edits made — not opening PR")
            return None

        pr_url = self.github.open_pr(
            repo,
            title=f"[autopilot] {issue_description[:60]}",
            body=f"## Autopilot Fix\n\n**Issue:** {issue_description}\n\n**Branch:** `{branch}`\n\n---\n\nThis PR was generated by [truesight_autopilot](https://github.com/TrueSightDAO/truesight_autopilot). Please review before merging.",
            head=branch,
            base="main",
        )

        if pr_url:
            edgar = EdgarLogger()
            edgar.log_contribution(
                minutes=5,
                description=f"[autopilot] {repo}: {issue_description[:100]}",
                pr_url=pr_url,
            )

        return pr_url

    def run(self, repo: str, diagnosis: dict[str, str]) -> str | None:
        """Run the fix loop and return PR URL, or None if no fix was made."""
        if settings.dry_run:
            logger.info(
                "[dry-run] would fix %s: %s",
                repo,
                diagnosis.get("root_cause", "N/A"),
            )
            return None

        branch = f"autopilot/fix-{int(time.time())}"
        if not self.github.create_branch(repo, "main", branch):
            logger.error("Failed to create branch on %s", repo)
            return None

        logger.info("Fix loop started: repo=%s branch=%s", repo, branch)

        system = self._build_system_prompt(repo, diagnosis)
        tools = self._tool_schemas()
        messages: list[dict[str, Any]] = []

        edits_made = False

        for step in range(self.max_iterations):
            logger.info("Fix iteration %d/%d", step + 1, self.max_iterations)
            try:
                completion = self.llm.chat(system, messages, tools=tools)
            except Exception as e:
                logger.error("LLM chat failed at step %d: %s", step, e)
                break

            assistant = completion.get("choices", [{}])[0].get("message", {})
            content = assistant.get("content", "")
            tool_calls = assistant.get("tool_calls", [])

            if not tool_calls:
                # Agent thinks it's done
                logger.info("Agent finished at step %d", step)
                break

            # Record assistant turn
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            # Execute each tool call with safety hooks
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    func_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "Error: invalid JSON arguments",
                    })
                    continue

                # Safety hook
                args_str = json.dumps(func_args)
                danger = _is_dangerous(args_str)
                if danger:
                    logger.warning("Safety hook blocked %s: %s", func_name, danger)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"BLOCKED by safety hook: {danger} detected in arguments",
                    })
                    continue

                # Execute
                try:
                    result = self._execute_tool(repo, branch, func_name, func_args)
                except Exception as e:
                    result = f"Tool error: {e}"
                    logger.error("Tool %s failed: %s", func_name, e)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

                if func_name == "edit_file" and "updated successfully" in result:
                    edits_made = True

        if not edits_made:
            logger.info("No edits made — not opening PR")
            return None

        pr_url = self.github.open_pr(
            repo,
            title=f"[autopilot] {diagnosis.get('root_cause', 'Fix')[:60]}",
            body=self._build_pr_body(diagnosis, branch),
            head=branch,
            base="main",
        )

        if pr_url:
            edgar = EdgarLogger()
            edgar.log_contribution(
                minutes=5,
                description=f"[autopilot] {repo}: {diagnosis.get('root_cause', 'fix')[:100]}",
                pr_url=pr_url,
            )

        return pr_url

    # ───────────────────────── System Prompt ─────────────────────────

    def _build_system_prompt(self, repo: str, diagnosis: dict[str, str]) -> str:
        repos = ", ".join(settings.allowed_repos)
        return (
            "You are an autonomous code fixer for TrueSight DAO.\n\n"
            f"Repo: {repo}\n"
            f"Root cause: {diagnosis.get('root_cause', 'Unknown')}\n"
            f"Proposed fix: {diagnosis.get('proposed_fix', 'Unknown')}\n"
            f"Files to edit: {diagnosis.get('files_to_edit', 'Unknown')}\n\n"
            f"Allowed repos (you can read/write to all of these): {repos}\n\n"
            "Rules:\n"
            "1. Read files before editing to understand context.\n"
            "2. Make MINIMAL changes — fix only what's broken.\n"
            "3. After editing a Python file, run py_compile to validate syntax.\n"
            "4. If the fix requires creating a new file, use create_file.\n"
            "5. If you need to delete a file, use delete_file.\n"
            "6. If you're stuck after 3 attempts, say 'I give up' and stop.\n"
            "7. When done, do not call any more tools.\n"
        )

    # ───────────────────────── Tool Schemas ─────────────────────────

    def _tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from any TrueSightDAO repo.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "path": {
                                "type": "string",
                                "description": "File path relative to repo root, e.g. 'scripts/detect_circle_hosting_retailers.py'",
                            },
                        },
                        "required": ["repo", "path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "description": "Replace a string in a file on the branch. old_string must match exactly.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "path": {"type": "string"},
                            "old_string": {
                                "type": "string",
                                "description": "Exact text to replace. Include enough context for uniqueness.",
                            },
                            "new_string": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                        },
                        "required": ["repo", "path", "old_string", "new_string"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grep_code",
                    "description": "Search for a pattern across any TrueSightDAO repo.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "pattern": {"type": "string"},
                            "path": {
                                "type": "string",
                                "description": "Optional subdirectory, e.g. 'scripts/'",
                            },
                        },
                        "required": ["repo", "pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_file",
                    "description": "Create a new file on the branch.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "path": {
                                "type": "string",
                                "description": "File path relative to repo root",
                            },
                            "content": {
                                "type": "string",
                                "description": "File content",
                            },
                        },
                        "required": ["repo", "path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Delete a file from the branch.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "path": {
                                "type": "string",
                                "description": "File path relative to repo root",
                            },
                        },
                        "required": ["repo", "path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "py_compile",
                    "description": "Run Python syntax check on a file by downloading and checking locally.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "repo": {
                                "type": "string",
                                "description": "Repo name under TrueSightDAO",
                            },
                            "path": {
                                "type": "string",
                                "description": "Python file path relative to repo root",
                            },
                        },
                        "required": ["repo", "path"],
                    },
                },
            },
        ]

    # ───────────────────────── Tool Execution ─────────────────────────

    def _execute_tool(
        self, repo: str, branch: str, func_name: str, args: dict[str, Any]
    ) -> str:
        target_repo = args.get("repo", repo)
        if target_repo not in settings.allowed_repos:
            return f"Error: repo '{target_repo}' is not in ALLOWED_REPOS. Allowed: {', '.join(settings.allowed_repos)}"

        if func_name == "read_file":
            return self._tool_read_file(target_repo, args["path"], branch)
        if func_name == "edit_file":
            return self._tool_edit_file(
                target_repo, branch, args["path"], args["old_string"], args["new_string"]
            )
        if func_name == "create_file":
            return self._tool_create_file(target_repo, branch, args["path"], args["content"])
        if func_name == "delete_file":
            return self._tool_delete_file(target_repo, branch, args["path"])
        if func_name == "grep_code":
            return self._tool_grep_code(target_repo, args["pattern"], args.get("path"))
        if func_name == "py_compile":
            return self._tool_py_compile(target_repo, branch, args["path"])
        return f"Unknown tool: {func_name}"

    def _tool_read_file(self, repo: str, path: str, branch: str) -> str:
        result = self.github.read_file(repo, path, ref=branch)
        if result.get("type") == "file":
            return result["content"]
        if result.get("type") == "directory":
            entries = "\n".join(f"- {e['name']} ({e['type']})" for e in result.get("entries", []))
            return f"Directory listing:\n{entries}"
        return f"Error: {result.get('error', 'unknown')}"

    def _tool_edit_file(
        self, repo: str, branch: str, path: str, old_string: str, new_string: str
    ) -> str:
        result = self.github.read_file(repo, path, ref=branch)
        if result.get("type") != "file":
            return f"Error reading file: {result.get('error', 'unknown')}"

        content = result["content"]
        if old_string not in content:
            return "Error: old_string not found in file. The text must match exactly."

        new_content = content.replace(old_string, new_string, 1)
        if new_content == content:
            return "Error: replacement did not change anything"

        ok = self.github.commit_file(
            repo,
            branch,
            path,
            new_content,
            message=f"[autopilot] Fix {path}",
        )
        return "File updated successfully" if ok else "Failed to commit file"

    def _tool_grep_code(self, repo: str, pattern: str, path: str | None) -> str:
        from .tools.github_tools import search_codebase

        query = f"repo:TrueSightDAO/{repo} {pattern}"
        if path:
            query += f" path:{path}"
        result = search_codebase(repo, pattern)  # search_codebase already adds repo
        if result.get("type") == "search_results":
            items = result.get("items", [])
            if not items:
                return "No matches found"
            lines = [f"- {i['path']}: {i['url']}" for i in items[:20]]
            return f"Found {len(items)} match(es):\n" + "\n".join(lines)
        return f"Error: {result.get('error', 'unknown')}"

    def _tool_create_file(self, repo: str, branch: str, path: str, content: str) -> str:
        ok = self.github.commit_file(
            repo, branch, path, content,
            message=f"[autopilot] Create {path}",
        )
        return "File created successfully" if ok else "Failed to create file"

    def _tool_delete_file(self, repo: str, branch: str, path: str) -> str:
        ok = self.github.delete_file(repo, branch, path)
        return "File deleted successfully" if ok else "Failed to delete file"

    def _tool_py_compile(self, repo: str, branch: str, path: str) -> str:
        result = self.github.read_file(repo, path, ref=branch)
        if result.get("type") != "file":
            return f"Error reading file: {result.get('error', 'unknown')}"

        # Write to temp file and compile
        suffix = Path(path).suffix or ".py"
        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
            f.write(result["content"])
            tmp_path = f.name

        try:
            py_compile.compile(tmp_path, doraise=True)
            return "Syntax OK"
        except py_compile.PyCompileError as e:
            return f"Syntax error: {e}"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ───────────────────────── PR Body ─────────────────────────

    def _build_pr_body(self, diagnosis: dict[str, str], branch: str) -> str:
        return (
            "## Autopilot Fix\n\n"
            f"**Root cause:** {diagnosis.get('root_cause', 'Unknown')}\n\n"
            f"**Proposed fix:** {diagnosis.get('proposed_fix', 'Unknown')}\n\n"
            f"**Files edited:** {diagnosis.get('files_to_edit', 'Unknown')}\n\n"
            f"**Branch:** `{branch}`\n\n"
            "---\n\n"
            "This PR was generated by [truesight_autopilot](https://github.com/TrueSightDAO/truesight_autopilot). "
            "Please review before merging.\n"
        )
