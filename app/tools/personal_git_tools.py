"""Sophia tool: push to a contributor's personal (non-DAO) repo.

Reads the opt-in registry at ``TrueSightDAO/agentic_ai_context/PERSONAL_CONTRIBUTOR_BACKLOGS.md``,
cross-checks the calling governor's identity against the row registered there, and — only on an
exact match between (a) who is asking, (b) the repo they're asking to push to, and (c) what's
registered — pulls the named credential from the vault server-side (never returned to chat, never
logged, never placed in a subprocess argv or the cloned repo's persisted git config) to push a
branch and open a PR against that contributor's own private repo.

Deliberately separate from ``git_tools.py``: that tool's guardrails (``allowed_repos``, the DAO
SSH key) assume every repo is DAO-owned and any governor may act on it. Personal repos are the
opposite — exactly one contributor may ever be the target, and the credential is theirs, not
Sophia's own DAO key. Small helpers are duplicated rather than imported from ``git_tools`` to
avoid coupling to its private internals or risking its existing, tested behavior.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..tool_registry import ToolSpec
from ..vault import get_vault
from .github_tools import read_repo_file
from .vault_tools import VAULT_URL

logger = logging.getLogger("autopilot.tools.personal_git_tools")

_GIT_AUTHOR_NAME = "Sophia (TrueSight Autopilot)"
_GIT_AUTHOR_EMAIL = "sophia@truesight.me"
_REGISTRY_REPO = "agentic_ai_context"
_REGISTRY_PATH = "PERSONAL_CONTRIBUTOR_BACKLOGS.md"
_CLONE_TIMEOUT = 180
_PUSH_TIMEOUT = 180
_MAX_ERR_CHARS = 2000


def _err(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "reason": reason, **extra}


def _remote_url(target_repo: str) -> str:
    """HTTPS remote for a personal repo. Patchable in tests (file:// URLs)."""
    return f"https://github.com/{target_repo}.git"


def _redact(text: str, token: str) -> str:
    """Defense in depth: strip a literal token from any text before it can be returned/logged."""
    if not text or not token:
        return text
    return text.replace(token, "***REDACTED***")


def _run_git(
    args: list[str],
    cwd: str | Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _safe_repo_path(workdir: Path, rel_path: str) -> Path | None:
    """Resolve ``rel_path`` inside the clone; reject traversal outside it."""
    if not rel_path or rel_path.startswith("/"):
        return None
    p = (workdir / rel_path).resolve()
    try:
        p.relative_to(workdir.resolve())
    except ValueError:
        return None
    if ".git" in p.relative_to(workdir.resolve()).parts:
        return None
    return p


def _parse_registry_row(markdown: str, contributor: str) -> dict[str, str] | None:
    """Find the row for ``contributor`` in the registry table.

    Table shape (``PERSONAL_CONTRIBUTOR_BACKLOGS.md`` § Registry)::

        | Contributor | Backlog repo | Format | Vault credential name |

    Exact match on the Contributor cell. Returns ``{"repo": "owner/name", "credential_name":
    str}`` or ``None`` if there's no row for them, or the matched row doesn't have a parseable
    repo + credential name (e.g. a placeholder like "not yet wired up").
    """
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if not cells[0] or set(cells[0]) <= {"-"} or cells[0] == "Contributor":
            continue  # header / separator row
        if cells[0] != contributor:
            continue
        repo_match = re.search(r"github\.com/([\w.-]+/[\w.-]+)", cells[1])
        cred_match = re.search(r"`([A-Za-z0-9_]+)`", cells[3])
        if not repo_match or not cred_match:
            return None
        return {"repo": repo_match.group(1), "credential_name": cred_match.group(1)}
    return None


def push_to_personal_repo(
    governor_name: str,
    target_repo: str,
    branch: str,
    commit_message: str,
    writes: list[dict] | None = None,
    edits: list[dict] | None = None,
    deletes: list[str] | None = None,
    pr_title: str = "",
    pr_body: str = "",
    open_pr: bool = True,
) -> dict[str, Any]:
    """Push to ``target_repo`` on behalf of ``governor_name`` — only if the registry says
    that's their repo. See module docstring for the identity/credential model."""
    if not governor_name:
        return _err(
            "no calling identity available — refusing (personal repos require a known governor)"
        )
    if not target_repo or not branch or not commit_message:
        return _err("target_repo, branch, and commit_message are required")
    if branch in {"main", "master"}:
        return _err(
            "refusing to push to a default branch — pick a feature branch; merges go through a PR",
            branch=branch,
        )
    if not (writes or edits or deletes):
        return _err("nothing to do: provide writes, edits, and/or deletes")

    registry = read_repo_file(_REGISTRY_REPO, _REGISTRY_PATH, "main")
    if registry.get("type") != "file":
        return _err(
            "could not read the personal-backlog registry",
            detail=registry.get("error") or str(registry),
        )
    row = _parse_registry_row(registry["content"], governor_name)
    if row is None:
        return _err(
            f"no registry entry for '{governor_name}' in {_REGISTRY_PATH} — they need to add "
            "themselves (their own private repo) to the registry first",
            registry_path=_REGISTRY_PATH,
        )
    if row["repo"] != target_repo:
        return _err(
            f"'{target_repo}' does not match the repo registered for '{governor_name}' "
            f"({row['repo']}) — refusing to push to an unregistered repo",
            registered_repo=row["repo"],
        )

    credential_name = row["credential_name"]
    vault = get_vault()
    if not vault.is_initialized() or not vault.has_credential(credential_name):
        return _err(
            f"credential '{credential_name}' is not in the vault yet — add it at {VAULT_URL} "
            "before this will work",
            credential_name=credential_name,
        )
    token = vault.get_value(credential_name)  # server-side only — never logged or returned below

    workdir = Path(tempfile.mkdtemp(prefix="sophia-personal-git-"))
    try:
        remote_url = _remote_url(target_repo)
        # Token flows via GIT_CONFIG_* env vars only — never argv (visible via `ps` to anyone on
        # the box), never a URL embedded in the clone (which git would otherwise persist into
        # the checkout's .git/config on disk). Skipped for file:// remotes (tests) since there's
        # no auth to inject and a local path can't safely accept an Authorization header.
        auth_env = (
            {}
            if remote_url.startswith("file://")
            else {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

        clone = workdir / "repo"
        try:
            r = _run_git(
                ["clone", "--depth", "1", remote_url, str(clone)],
                cwd=workdir,
                extra_env=auth_env,
                timeout=_CLONE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return _err(f"git clone timed out after {_CLONE_TIMEOUT}s", repo=target_repo)
        if r.returncode != 0:
            return _err(
                "git clone failed",
                repo=target_repo,
                stderr=_redact(r.stderr, token)[-_MAX_ERR_CHARS:],
            )

        head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=clone)
        default_branch = head.stdout.strip() or "main"
        if branch in {default_branch, "main", "master"}:
            return _err(
                "refusing to push to a default branch — pick a feature branch",
                branch=branch,
                default_branch=default_branch,
            )

        r = _run_git(["checkout", "-b", branch], cwd=clone)
        if r.returncode != 0:
            return _err("git checkout -b failed", stderr=r.stderr[-_MAX_ERR_CHARS:])

        applied: list[str] = []
        for w in writes or []:
            path, content = w.get("path", ""), w.get("content", "")
            target = _safe_repo_path(clone, path)
            if target is None:
                return _err("invalid write path", path=path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            applied.append(f"write {path}")

        for e in edits or []:
            path = e.get("path", "")
            search, replace = e.get("search", ""), e.get("replace", "")
            target = _safe_repo_path(clone, path)
            if target is None or not target.is_file():
                return _err("edit target not found", path=path)
            if not search:
                return _err("edit needs a non-empty search string", path=path)
            text = target.read_text(encoding="utf-8")
            count = text.count(search)
            if count == 0:
                return _err(
                    "search string not found in file",
                    path=path,
                    search_preview=search[:120],
                )
            if count > 1 and not e.get("replace_all"):
                return _err(
                    f"search string occurs {count} times; make it more specific or pass replace_all=true",
                    path=path,
                )
            target.write_text(
                text.replace(search, replace)
                if e.get("replace_all")
                else text.replace(search, replace, 1),
                encoding="utf-8",
            )
            applied.append(f"edit {path}")

        for d in deletes or []:
            target = _safe_repo_path(clone, d)
            if target is None or not target.exists():
                return _err("delete target not found", path=d)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            applied.append(f"delete {d}")

        _run_git(["add", "-A"], cwd=clone)
        r = _run_git(
            [
                "-c",
                f"user.name={_GIT_AUTHOR_NAME}",
                "-c",
                f"user.email={_GIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                commit_message,
            ],
            cwd=clone,
        )
        if r.returncode != 0:
            return _err(
                "git commit failed (no effective changes?)",
                stderr=(r.stderr or r.stdout)[-_MAX_ERR_CHARS:],
            )
        commit_sha = _run_git(["rev-parse", "HEAD"], cwd=clone).stdout.strip()

        try:
            r = _run_git(
                ["push", "origin", f"HEAD:refs/heads/{branch}"],
                cwd=clone,
                extra_env=auth_env,
                timeout=_PUSH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return _err(f"git push timed out after {_PUSH_TIMEOUT}s")
        if r.returncode != 0:
            return _err("git push failed", stderr=_redact(r.stderr, token)[-_MAX_ERR_CHARS:])

        result: dict[str, Any] = {
            "status": "success",
            "repo": target_repo,
            "branch": branch,
            "base_branch": default_branch,
            "commit_sha": commit_sha,
            "applied": applied,
        }

        if open_pr:
            try:
                resp = httpx.post(
                    f"https://api.github.com/repos/{target_repo}/pulls",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Authorization": f"Bearer {token}",
                    },
                    json={
                        "title": pr_title or commit_message,
                        "head": branch,
                        "base": default_branch,
                        "body": pr_body
                        or f"Opened by Sophia (truesight_autopilot) on behalf of {governor_name}.\n\n{commit_message}",
                    },
                    timeout=20.0,
                )
                resp.raise_for_status()
                result["pr_url"] = resp.json().get("html_url", "")
            except httpx.HTTPStatusError as exc:
                result["pr_error"] = (
                    f"branch pushed but PR creation failed ({exc.response.status_code}): "
                    f"{_redact(exc.response.text, token)[:300]}"
                )
            except httpx.RequestError as exc:
                result["pr_error"] = f"branch pushed but PR creation failed: {exc}"

        logger.info(
            "push_to_personal_repo: %s -> %s/%s (%d changes, pr=%s)",
            governor_name,
            target_repo,
            branch,
            len(applied),
            result.get("pr_url", "-"),
        )
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _handler(args: dict, ctx: dict) -> str:
    return json.dumps(
        push_to_personal_repo(
            governor_name=ctx.get("governor_name", ""),
            target_repo=args.get("target_repo", ""),
            branch=args.get("branch", ""),
            commit_message=args.get("commit_message", ""),
            writes=args.get("writes"),
            edits=args.get("edits"),
            deletes=args.get("deletes"),
            pr_title=args.get("pr_title", ""),
            pr_body=args.get("pr_body", ""),
            open_pr=args.get("open_pr", True),
        ),
        indent=2,
    )


TOOL_SPEC = ToolSpec(
    name="push_to_personal_repo",
    description=(
        "Push to a governor's own PERSONAL (non-DAO) private repo — e.g. a personal market-"
        "analysis backlog — using a credential they've stored in the vault under their name. "
        "ONLY works if the calling governor has an entry in "
        "agentic_ai_context/PERSONAL_CONTRIBUTOR_BACKLOGS.md whose repo matches target_repo; "
        "refuses otherwise. Never push to a default branch — always a feature branch + PR. "
        "Use this ONLY when a governor explicitly flags something as personal work they want "
        "logged (e.g. market/trading analysis) — never for DAO work, and never speculatively."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_repo": {
                "type": "string",
                "description": "The personal repo to push to, as 'owner/name' (must match the "
                "calling governor's registry entry exactly).",
            },
            "branch": {
                "type": "string",
                "description": "Feature branch to create/push, e.g. 'log/2026-07-21'. Must not "
                "be the default branch.",
            },
            "commit_message": {
                "type": "string",
                "description": "Commit message (used as PR title when pr_title is omitted).",
            },
            "writes": {
                "type": "array",
                "description": "Full-content file writes: [{path, content}]. Creates parent "
                "dirs; overwrites existing files.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
            "edits": {
                "type": "array",
                "description": "Search/replace hunks: [{path, search, replace, replace_all?}]. "
                "search must occur exactly once unless replace_all is true.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "search": {"type": "string"},
                        "replace": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                },
            },
            "deletes": {
                "type": "array",
                "description": "Repo-relative paths to delete.",
                "items": {"type": "string"},
            },
            "pr_title": {
                "type": "string",
                "description": "PR title (default: commit_message).",
            },
            "pr_body": {
                "type": "string",
                "description": "PR body — explain what was logged and why.",
            },
            "open_pr": {
                "type": "boolean",
                "description": "Open a PR after pushing (default true).",
                "default": True,
            },
        },
        "required": ["target_repo", "branch", "commit_message"],
    },
    handler=_handler,
    default_roles=frozenset({"infrastructure"}),
)
