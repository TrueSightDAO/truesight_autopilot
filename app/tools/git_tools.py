"""Native git tool: branch + edit + commit + push + PR for TrueSightDAO repos.

Closes OPEN_FOLLOW_UPS items 1/2/4 (agentic_ai_context, 2026-05-31): the
Contents-API tools could only *create* files and choked on anything >15KB
because the whole payload had to round-trip through the LLM call. This tool
does real git on the box instead:

- shallow-clones the repo to a temp dir,
- applies **writes** (full-content, for new/small files), **edits**
  (search/replace hunks — the LLM passes only the diff, so file size stops
  mattering), and **deletes**,
- commits as "Sophia (TrueSight Autopilot)", pushes a feature branch,
- opens a PR via the GitHub API and returns its URL.

Guardrails:
- repo must be in ``settings.allowed_repos`` (same gate as ``open_fix_pr``).
- the push target may NEVER be the repo's default branch (or main/master) —
  branch + PR always; merging stays behind ``merge_pr`` / a human.
- the PAT is fed to git via an inline credential helper reading an env var —
  it never lands in argv or on disk.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("autopilot.tools.git_tools")

_GIT_AUTHOR_NAME = "Sophia (TrueSight Autopilot)"
_GIT_AUTHOR_EMAIL = "sophia@truesight.me"
# Inline helper: git asks it for credentials; it answers from $GIT_PAT.
# Single-quoted shell function — the PAT stays in the subprocess env only.
_CREDENTIAL_HELPER = '!f() { echo "username=x-access-token"; echo "password=${GIT_PAT}"; }; f'
_CLONE_TIMEOUT = 180
_PUSH_TIMEOUT = 180
_MAX_ERR_CHARS = 2000


def _err(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "reason": reason, **extra}


def _remote_url(repo: str) -> str:
    """HTTPS remote for a TrueSightDAO repo. Patchable in tests (file:// URLs)."""
    return f"https://github.com/TrueSightDAO/{repo}.git"


def _git(args: list[str], cwd: str | Path, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_PAT"] = settings.github_pat or ""
    env["GIT_TERMINAL_PROMPT"] = "0"  # fail fast instead of hanging on a prompt
    return subprocess.run(
        ["git", "-c", f"credential.helper={_CREDENTIAL_HELPER}",
         "-c", f"user.name={_GIT_AUTHOR_NAME}", "-c", f"user.email={_GIT_AUTHOR_EMAIL}",
         *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
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


def git_push_changes(
    repo: str,
    branch: str,
    commit_message: str,
    writes: list[dict] | None = None,
    edits: list[dict] | None = None,
    deletes: list[str] | None = None,
    base_branch: str = "",
    pr_title: str = "",
    pr_body: str = "",
    open_pr: bool = True,
) -> dict[str, Any]:
    """Clone → branch → apply changes → commit → push → (optionally) open a PR.

    ``writes``: ``[{"path": ..., "content": ...}]`` — full-content file writes
    (creates parent dirs; overwrites if the file exists).
    ``edits``: ``[{"path": ..., "search": ..., "replace": ..., "replace_all": bool?}]``
    — exact-substring replacement. Without ``replace_all``, the search string
    must occur exactly once or the call fails (no silent wrong-spot edits).
    ``deletes``: list of repo-relative paths to remove.
    """
    if not repo or not branch or not commit_message:
        return _err("repo, branch, and commit_message are required")
    if repo not in settings.allowed_repos:
        return _err("repo not in allowed list", repo=repo, allowed=settings.allowed_repos)
    if not (writes or edits or deletes):
        return _err("nothing to do: provide writes, edits, and/or deletes")
    if not settings.github_pat:
        return _err("TRUESIGHT_DAO_AUTOPILOT PAT not configured on this host")

    workdir = Path(tempfile.mkdtemp(prefix=f"sophia-git-{repo}-"))
    try:
        # ── clone (default branch, or base_branch when given) ────────────
        clone_args = ["clone", "--depth", "1"]
        if base_branch:
            clone_args += ["--branch", base_branch]
        clone_args += [_remote_url(repo), str(workdir / "repo")]
        try:
            r = _git(clone_args, cwd=workdir, timeout=_CLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _err(f"git clone timed out after {_CLONE_TIMEOUT}s", repo=repo)
        if r.returncode != 0:
            return _err("git clone failed", repo=repo, stderr=r.stderr[-_MAX_ERR_CHARS:])
        clone = workdir / "repo"

        # ── never push to the default branch ──────────────────────────────
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=clone)
        default_branch = head.stdout.strip() or "main"
        if branch in {default_branch, "main", "master"}:
            return _err(
                "refusing to push to a default branch — pick a feature branch; "
                "merges go through a PR + merge_pr",
                branch=branch, default_branch=default_branch,
            )

        r = _git(["checkout", "-b", branch], cwd=clone)
        if r.returncode != 0:
            return _err("git checkout -b failed", stderr=r.stderr[-_MAX_ERR_CHARS:])

        # ── apply changes ─────────────────────────────────────────────────
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
                return _err("search string not found in file", path=path,
                            search_preview=search[:120])
            if count > 1 and not e.get("replace_all"):
                return _err(
                    f"search string occurs {count} times; make it more specific "
                    "or pass replace_all=true", path=path,
                )
            target.write_text(
                text.replace(search, replace) if e.get("replace_all")
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

        # ── commit + push ─────────────────────────────────────────────────
        _git(["add", "-A"], cwd=clone)
        r = _git(["commit", "-m", commit_message], cwd=clone)
        if r.returncode != 0:
            return _err("git commit failed (no effective changes?)",
                        stderr=(r.stderr or r.stdout)[-_MAX_ERR_CHARS:])
        commit_sha = _git(["rev-parse", "HEAD"], cwd=clone).stdout.strip()

        try:
            r = _git(["push", "origin", f"HEAD:refs/heads/{branch}"],
                     cwd=clone, timeout=_PUSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return _err(f"git push timed out after {_PUSH_TIMEOUT}s")
        if r.returncode != 0:
            return _err("git push failed", stderr=r.stderr[-_MAX_ERR_CHARS:])

        result: dict[str, Any] = {
            "status": "success",
            "repo": repo,
            "branch": branch,
            "base_branch": base_branch or default_branch,
            "commit_sha": commit_sha,
            "applied": applied,
        }

        # ── open PR ───────────────────────────────────────────────────────
        if open_pr:
            try:
                resp = httpx.post(
                    f"https://api.github.com/repos/TrueSightDAO/{repo}/pulls",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "Authorization": f"Bearer {settings.github_pat}",
                    },
                    json={
                        "title": pr_title or commit_message,
                        "head": branch,
                        "base": base_branch or default_branch,
                        "body": pr_body or f"Opened by Sophia (truesight_autopilot).\n\n{commit_message}",
                    },
                    timeout=20.0,
                )
                resp.raise_for_status()
                result["pr_url"] = resp.json().get("html_url", "")
            except httpx.HTTPStatusError as exc:
                result["pr_error"] = (
                    f"branch pushed but PR creation failed "
                    f"({exc.response.status_code}): {exc.response.text[:300]}"
                )
            except httpx.RequestError as exc:
                result["pr_error"] = f"branch pushed but PR creation failed: {exc}"

        logger.info("git_push_changes: %s -> %s (%d changes, pr=%s)",
                    repo, branch, len(applied), result.get("pr_url", "-"))
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="git_push_changes",
    description=(
        "Make real git changes to a TrueSightDAO repo: shallow-clone, create a "
        "feature branch, apply file changes, commit, push, and open a PR. "
        "Handles files of ANY size — for large files pass `edits` "
        "(search/replace hunks) instead of whole-file `writes`. Never pushes "
        "to main/master; merging still requires merge_pr or a human. Prefer "
        "this over upload_file_to_github whenever you are modifying existing "
        "code or need multiple files in one commit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repo name under TrueSightDAO (must be in the allowed list)."},
            "branch": {"type": "string", "description": "Feature branch to create/push, e.g. 'fix/partner-page-hero'. Must not be the default branch."},
            "commit_message": {"type": "string", "description": "Commit message (used as PR title when pr_title is omitted)."},
            "writes": {
                "type": "array",
                "description": "Full-content file writes: [{path, content}]. Creates parent dirs; overwrites existing files.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            "edits": {
                "type": "array",
                "description": "Exact-substring search/replace edits: [{path, search, replace, replace_all?}]. Without replace_all the search must match exactly once. Use this for large files — only the hunk travels through the call.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "search": {"type": "string"},
                        "replace": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "search", "replace"],
                },
            },
            "deletes": {
                "type": "array",
                "description": "Repo-relative paths to delete.",
                "items": {"type": "string"},
            },
            "base_branch": {"type": "string", "description": "Branch to base the work on (default: the repo's default branch)."},
            "pr_title": {"type": "string", "description": "PR title (default: commit_message)."},
            "pr_body": {"type": "string", "description": "PR body — explain goal, changes, testing."},
            "open_pr": {"type": "boolean", "description": "Open a PR after pushing (default true).", "default": True},
        },
        "required": ["repo", "branch", "commit_message"],
    },
    handler=lambda args, ctx: json.dumps(git_push_changes(
        repo=args.get("repo", ""),
        branch=args.get("branch", ""),
        commit_message=args.get("commit_message", ""),
        writes=args.get("writes"),
        edits=args.get("edits"),
        deletes=args.get("deletes"),
        base_branch=args.get("base_branch", ""),
        pr_title=args.get("pr_title", ""),
        pr_body=args.get("pr_body", ""),
        open_pr=args.get("open_pr", True),
    ), indent=2),
    default_roles=frozenset({"infrastructure"}),
)
