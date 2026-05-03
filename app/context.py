"""Context ingestion: build the system prompt from agentic_ai_context and related docs."""
from __future__ import annotations

from pathlib import Path

from .config import settings

CANONICAL_CONTEXT_FILES: list[str] = [
    "OPERATING_INSTRUCTIONS.md",
    "WORKSPACE_CONTEXT.md",
    "PROJECT_INDEX.md",
    "PURPOSE_AND_MISSION.md",
    "DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md",
    "GITHUB_AGENTIC_AI_SSH.md",
    "CMO_SETH_GODIN.md",
    "DR_MANHATTAN.md",
    "LEDGER_CONVERSION_AND_REPACKAGING.md",
    "SUPPLY_CHAIN_AND_FREIGHTING.md",
    "DAPP_PAGE_CONVENTIONS.md",
    "API_CREDENTIALS_DOCUMENTATION.md",
    "SETUP_REQUIREMENTS.md",
    "AUTOPILOT_CODE_MODIFICATIONS.md",
]

_SYSTEM_PROMPT_HEADER = """You are the TrueSight DAO Autopilot — an autonomous SRE and developer assistant.
You have full read access to the workspace context and can execute approved actions on behalf of verified governors.

## RULES
1. Always answer based on the provided context. If the answer is not in context, say "I don't have that in my context."
2. For code changes, stop at PR creation unless explicitly told to merge.
3. Never expose secrets, .env values, credentials, or private keys in responses.
4. If unsure, ask the governor rather than guess.
5. Be concise but thorough. Prefer bullet points for lists.
6. When discussing the DAO's mission, reference PURPOSE_AND_MISSION.md.
7. When discussing marketing, reference CMO_SETH_GODIN.md principles.
8. When discussing strategy, reference DR_MANHATTAN.md principles.

## AVAILABLE TOOLS
- list_org_repos() — list all repos in TrueSightDAO org (use to discover repos)
- read_context_file(path) — read a file from agentic_ai_context
- read_repo_file(repo, path, ref="main") — read a file from a GitHub repo (content API, no clone)
- open_fix_pr(repo, issue_description) — diagnose and open a fix PR via agentic loop

## AUTOPILOT MODE
When the governor asks you to fix something, create something, or check infrastructure:
1. Gather context (read relevant files)
2. Plan the fix
3. Open a PR with the changes
4. Report the PR URL

## RESPONSE FORMAT
Respond in plain text. If you need to propose an action, wrap it in a JSON block like:
```json
{"proposal": {"action": "open_fix_pr", "repo": "go_to_market", ...}}
```
The UI will render this as an approval card.

---
"""


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"<!-- Error reading {path}: {e} -->\n"


def build_system_prompt() -> str:
    """Build the full system prompt from canonical context files."""
    parts = [_SYSTEM_PROMPT_HEADER]

    repo_dir = settings.context_repos_dir / "agentic_ai_context"
    if not repo_dir.exists():
        workspace_root = Path(__file__).resolve().parents[3]
        repo_dir = workspace_root / "agentic_ai_context"

    for filename in CANONICAL_CONTEXT_FILES:
        file_path = repo_dir / filename
        if file_path.exists():
            parts.append(f"\n## FILE: {filename}\n")
            parts.append(_read_file(file_path))
        else:
            parts.append(f"\n## FILE: {filename} (NOT FOUND)\n")

    return "\n".join(parts)


def get_context_file(path: str) -> str | None:
    """Read a specific file from the synced agentic_ai_context repo."""
    repo_dir = settings.context_repos_dir / "agentic_ai_context"
    if not repo_dir.exists():
        workspace_root = Path(__file__).resolve().parents[3]
        repo_dir = workspace_root / "agentic_ai_context"

    target = repo_dir / path
    try:
        target = target.resolve()
        repo_dir = repo_dir.resolve()
        if not str(target).startswith(str(repo_dir)):
            return None
        if target.exists() and target.is_file():
            return target.read_text(encoding="utf-8")
    except Exception:
        pass
    return None


_cached_system_prompt: str | None = None


def get_system_prompt() -> str:
    global _cached_system_prompt
    if _cached_system_prompt is None:
        _cached_system_prompt = build_system_prompt()
    return _cached_system_prompt


def refresh_system_prompt() -> str:
    global _cached_system_prompt
    _cached_system_prompt = build_system_prompt()
    return _cached_system_prompt
