"""GitHub API tools for reading code from TrueSightDAO repos."""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..config import settings


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_pat:
        headers["Authorization"] = f"Bearer {settings.github_pat}"
    return headers


def read_repo_file(repo: str, path: str, ref: str = "main") -> dict[str, Any]:
    url = f"https://api.github.com/repos/TrueSightDAO/{repo}/contents/{path}"
    params = {"ref": ref}

    try:
        resp = httpx.get(url, headers=_github_headers(), params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            return {
                "type": "directory",
                "entries": [
                    {
                        "name": item.get("name"),
                        "type": item.get("type"),
                        "path": item.get("path"),
                    }
                    for item in data
                ],
                "url": str(resp.url),
            }

        content = data.get("content", "")
        encoding = data.get("encoding", "")
        if encoding == "base64" and content:
            decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        else:
            decoded = content

        return {
            "type": "file",
            "content": decoded,
            "size": data.get("size", 0),
            "url": data.get("html_url", ""),
            "encoding": encoding,
        }
    except httpx.HTTPStatusError as exc:
        return {
            "type": "error",
            "error": f"GitHub API error {exc.response.status_code}: {exc.response.text[:200]}",
        }
    except httpx.RequestError as exc:
        return {
            "type": "error",
            "error": f"Request failed: {exc}",
        }


def search_codebase(repo: str | None, query: str) -> dict[str, Any]:
    """GitHub code search. With a repo, scoped to TrueSightDAO/<repo>;
    without one, searches the whole TrueSightDAO org."""
    url = "https://api.github.com/search/code"
    scope = f"repo:TrueSightDAO/{repo}" if repo else "org:TrueSightDAO"
    params = {"q": f"{scope} {query}"}

    try:
        resp = httpx.get(url, headers=_github_headers(), params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return {
            "type": "search_results",
            "total_count": data.get("total_count", 0),
            "items": [
                {
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "url": item.get("html_url"),
                }
                for item in data.get("items", [])
            ],
        }
    except httpx.HTTPStatusError as exc:
        return {
            "type": "error",
            "error": f"GitHub API error {exc.response.status_code}: {exc.response.text[:200]}",
        }
    except httpx.RequestError as exc:
        return {
            "type": "error",
            "error": f"Request failed: {exc}",
        }


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

_ALLOWED_CHAT_REPOS = ", ".join(
    [
        "dapp_beta",
        "dapp_prod",
        "tokenomics",
        "truesight_me",
        "truesight_me_prod",
        "agroverse_shop",
        "agroverse_shop_prod",
        "dao_client",
        "market_research",
        "sentiment_importer",
        "truesight_autopilot",
        ".github",
        "agentic_ai_context",
        "agroverse-inventory",
        "dao_protocol",
    ]
)


def _list_org_repos_handler(args: dict, ctx: dict) -> str:
    from ..github_client import GitHubClient

    gh = GitHubClient()
    repos = gh.list_org_repos()
    if not repos:
        return "Failed to list repos or none found."
    lines = [
        f"- {r['name']} ({'private' if r['private'] else 'public'}) — {r['description']}"
        for r in repos
    ]
    return "TrueSightDAO repositories:\n" + "\n".join(lines)


def _read_context_file_handler(args: dict, ctx: dict) -> str:
    from ..context import get_context_file

    result = get_context_file(args.get("path", ""))
    return result if result else "File not found."


def _read_repo_file_handler(args: dict, ctx: dict) -> str:
    result = read_repo_file(
        args.get("repo", ""), args.get("path", ""), args.get("ref", "main")
    )
    if result.get("type") == "file":
        return result["content"]
    if result.get("type") == "directory":
        return "Directory listing:\n" + "\n".join(
            f"- {e['name']} ({e['type']})" for e in result.get("entries", [])
        )
    return f"Error: {result.get('error', 'unknown')}"


def _list_prs_handler(args: dict, ctx: dict) -> str:
    import json as _json

    from ..github_client import GitHubClient

    gh = GitHubClient()
    repo_name = args.get("repo", "")
    state = args.get("state", "all")
    limit = int(args.get("limit", 20))
    prs = gh.list_prs(repo_name, state=state, limit=limit)
    return _json.dumps(
        {
            "status": "ok",
            "repo": repo_name,
            "state": state,
            "count": len(prs),
            "prs": prs,
        },
        indent=2,
    )


def _merge_pr_handler(args: dict, ctx: dict) -> str:
    from ..config import settings
    from ..github_client import GitHubClient

    repo_name = args.get("repo", "")
    pr_number = args.get("pr_number", 0)
    merge_method = args.get("merge_method", "squash")
    if repo_name not in settings.allowed_repos:
        return f"Error: repo '{repo_name}' not in allowed list."
    if repo_name in settings.prod_repos:
        return (
            f"Refused: '{repo_name}' is a PRODUCTION repo (beta-first rule). "
            f"Changes land in '{settings.prod_repos[repo_name]}'; promotion to "
            "prod is via sync_beta_to_prod on the governor's explicit approval, "
            "not PR merges on prod."
        )
    if repo_name in settings.api_only_repos:
        return f"Refused: '{repo_name}' is an API-only data repo (machine-owned); agents do not merge PRs there."
    if not pr_number:
        return "Error: pr_number is required."
    gh = GitHubClient()
    result = gh.merge_pr(repo_name, int(pr_number), merge_method)
    if result["merged"]:
        return f"✅ PR #{pr_number} on {repo_name} merged successfully (sha: {result['sha']}). {result['message']}"
    return f"❌ Failed to merge PR #{pr_number} on {repo_name}: {result['message']}"


def _mark_pr_ready_handler(args: dict, ctx: dict) -> str:
    import json as _json

    from ..config import settings
    from ..github_client import GitHubClient

    repo_name = args.get("repo", "")
    pr_number = args.get("pr_number", 0)
    if repo_name not in settings.allowed_repos:
        return _json.dumps(
            {"status": "error", "reason": f"repo '{repo_name}' not in allowed list"}
        )
    if not pr_number:
        return _json.dumps({"status": "error", "reason": "pr_number is required"})
    gh = GitHubClient()
    result = gh.mark_pr_ready_for_review(repo_name, int(pr_number))
    return _json.dumps(result, indent=2)


def _search_code_handler(args: dict, ctx: dict) -> str:
    import json as _json

    result = search_codebase(args.get("repo") or None, args.get("query", ""))
    return _json.dumps(result, indent=2)


TOOL_SPECS = [
    ToolSpec(
        name="search_code",
        description=(
            "Search file CONTENTS across TrueSightDAO GitHub repos (code search API). "
            "Omit 'repo' to search the entire org — use after search_context when a "
            "governor's term isn't in agentic_ai_context but may live in a project repo "
            "(scripts, GAS, sheets tooling). Returns matching file paths + URLs; follow "
            "up with read_repo_file. Note: GitHub only indexes default branches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Term or phrase to search for.",
                },
                "repo": {
                    "type": "string",
                    "description": "Optional repo name under TrueSightDAO; omit for org-wide.",
                },
            },
            "required": ["query"],
        },
        handler=_search_code_handler,
    ),
    ToolSpec(
        name="list_org_repos",
        description="List all repositories in the TrueSightDAO GitHub organization. Use this to discover what repos exist.",
        parameters={"type": "object", "properties": {}},
        handler=_list_org_repos_handler,
    ),
    ToolSpec(
        name="read_context_file",
        description="Read a file from the agentic_ai_context repository.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside agentic_ai_context, e.g. 'WORKSPACE_CONTEXT.md'",
                }
            },
            "required": ["path"],
        },
        handler=_read_context_file_handler,
    ),
    ToolSpec(
        name="read_repo_file",
        description="Read a file from a TrueSightDAO GitHub repository.",
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": f"GitHub repo name under TrueSightDAO. Allowed: {_ALLOWED_CHAT_REPOS}",
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
        handler=_read_repo_file_handler,
    ),
    ToolSpec(
        name="list_prs",
        description="List recent pull requests on a TrueSightDAO repo.",
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo name under TrueSightDAO.",
                },
                "state": {
                    "type": "string",
                    "description": "open, closed, or all.",
                    "enum": ["open", "closed", "all"],
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max PRs to return.",
                    "default": 20,
                },
            },
            "required": ["repo"],
        },
        handler=_list_prs_handler,
    ),
    ToolSpec(
        name="merge_pr",
        description="Merge a pull request. Only use when a governor explicitly tells you to merge. Auto-promotes draft PRs to ready-for-review before merging — you do NOT need a separate mark_pr_ready_for_review call before merging, just call merge_pr.",
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo name under TrueSightDAO.",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "The pull request number to merge.",
                },
                "merge_method": {
                    "type": "string",
                    "description": "squash (default), merge, or rebase.",
                    "enum": ["squash", "merge", "rebase"],
                    "default": "squash",
                },
            },
            "required": ["repo", "pr_number"],
        },
        handler=_merge_pr_handler,
    ),
    ToolSpec(
        name="mark_pr_ready_for_review",
        description="Promote a draft pull request to 'ready for review' (the inverse of opening as draft). Useful when you want to signal the PR is ready for human review without merging it yet. Note: merge_pr already auto-promotes drafts, so call this only when you want to mark ready WITHOUT merging.",
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo name under TrueSightDAO.",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "The pull request number to promote.",
                },
            },
            "required": ["repo", "pr_number"],
        },
        handler=_mark_pr_ready_handler,
    ),
]
