"""Tools to upload files to TrueSightDAO GitHub repos via the Contents API.

Two surfaces:
- ``upload_file_to_github(repo, path, content=...)`` — text-or-base64 content
  passed in the call.
- ``upload_local_file_to_github(local_path, repo, path, message)`` — reads any
  local file (binary or text), base64-encodes, ships. The one-call workflow for
  attachments (e.g. a Telegram-uploaded JPG saved to /tmp).
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("autopilot.tools.upload_to_github")

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # GitHub's Contents-API hard limit is ~25 MB


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_pat:
        headers["Authorization"] = f"Bearer {settings.github_pat}"
    return headers


def upload_file_to_github(
    repo: str,
    path: str,
    content: str = "",
    message: str = "",
    branch: str = "main",
    content_base64: str | None = None,
) -> dict[str, Any]:
    """Upload a file to a TrueSightDAO GitHub repo using the Contents API.

    Pass either:
    - ``content`` (plain text) — auto base64-encoded, OR
    - ``content_base64`` (already base64-encoded bytes) — used for PDFs and
      other binary artifacts; takes precedence when both are provided.

    If the file already exists on the branch, its blob ``sha`` is fetched
    automatically and the call becomes an **update** (OPEN_FOLLOW_UPS item 4 —
    previously the API rejected updates with "sha wasn't supplied").

    Returns a dict with 'status' ('success' or 'error') and details.
    """
    if content_base64:
        encoded = content_base64
    else:
        encoded = base64.b64encode((content or "").encode("utf-8")).decode("utf-8")
    url = f"https://api.github.com/repos/TrueSightDAO/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": encoded,
        "branch": branch,
    }

    # Existing file? Fetch its sha so the PUT updates instead of failing.
    try:
        head = httpx.get(
            url, headers=_github_headers(), params={"ref": branch}, timeout=15.0
        )
        if head.status_code == 200 and isinstance(head.json(), dict):
            existing_sha = head.json().get("sha", "")
            if existing_sha:
                payload["sha"] = existing_sha
    except httpx.RequestError:
        pass  # create-path still works; an update will surface the API error below

    try:
        resp = httpx.put(url, headers=_github_headers(), json=payload, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "success",
            "action": "updated" if "sha" in payload else "created",
            "commit_sha": data.get("commit", {}).get("sha", ""),
            "content_url": data.get("content", {}).get("html_url", ""),
            "message": f"File uploaded to {repo}/{path} on branch '{branch}'.",
        }
    except httpx.HTTPStatusError as exc:
        return {
            "status": "error",
            "error": f"GitHub API error {exc.response.status_code}: {exc.response.text[:300]}",
        }
    except httpx.RequestError as exc:
        return {
            "status": "error",
            "error": f"Request failed: {exc}",
        }


def upload_local_file_to_github(
    local_path: str,
    repo: str,
    path: str,
    message: str,
    branch: str = "main",
) -> dict[str, Any]:
    """Read a local file (any type — binary OK), base64-encode, push to GitHub.

    The one-call workflow for shipping attachments (Telegram-uploaded JPGs,
    generated PDFs, etc.) into a repo. Reads up to 25 MB.

    Returns the same shape as :func:`upload_file_to_github`.
    """
    if not local_path:
        return {"status": "error", "error": "local_path is required"}
    if not os.path.isfile(local_path):
        return {"status": "error", "error": f"file not found: {local_path}"}
    try:
        size = os.path.getsize(local_path)
        if size > _MAX_UPLOAD_BYTES:
            return {
                "status": "error",
                "error": f"file is {size} bytes; GitHub Contents API limit is ~25 MB",
            }
        with open(local_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        return {"status": "error", "error": f"read failed: {e}"}

    logger.info(
        "upload_local_file_to_github: %s (%d bytes) -> %s/%s",
        local_path,
        size,
        repo,
        path,
    )
    return upload_file_to_github(
        repo=repo,
        path=path,
        message=message,
        branch=branch,
        content_base64=encoded,
    )


# ── capability manifest entries ───────────────────────────────────────────

import json as _json  # noqa: E402

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="upload_file_to_github",
        description=(
            "Create or update a file in a TrueSightDAO GitHub repo by passing the "
            "content in the call. For plain text, use `content`. For binary "
            "(PDF, image, anything not UTF-8) pass `content_base64` instead — "
            "the value must already be base64-encoded. If the file is already "
            "on disk locally, prefer `upload_local_file_to_github` (one call, "
            "no manual base64)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repo name under TrueSightDAO.",
                },
                "path": {
                    "type": "string",
                    "description": "Path inside the repo, e.g. 'reports/market_analysis.md'.",
                },
                "content": {
                    "type": "string",
                    "description": "Plain-text content (auto base64-encoded).",
                },
                "message": {
                    "type": "string",
                    "description": "Short one-line commit message (max 72 chars).",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name. Default: main",
                    "default": "main",
                },
                "content_base64": {
                    "type": "string",
                    "description": "Pre-base64-encoded content for binary uploads (JPGs, PDFs). Takes precedence over `content` when both are provided.",
                },
            },
            "required": ["repo", "path", "message"],
        },
        handler=lambda args, ctx: _json.dumps(
            upload_file_to_github(
                repo=args.get("repo", ""),
                path=args.get("path", ""),
                content=args.get("content", ""),
                message=args.get("message", ""),
                branch=args.get("branch", "main"),
                content_base64=args.get("content_base64"),
            ),
            indent=2,
        ),
    ),
    ToolSpec(
        name="upload_local_file_to_github",
        description=(
            "Read any local file (binary OK — JPG, PNG, PDF, ZIP, etc.) and push "
            "it to a TrueSightDAO GitHub repo as-is. The one-call workflow when "
            "an attachment is already on disk (e.g. a Telegram-uploaded image "
            "saved by the adapter to /tmp). 25 MB cap per file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "local_path": {
                    "type": "string",
                    "description": "Absolute path to the file on the autopilot host.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repo name under TrueSightDAO.",
                },
                "path": {
                    "type": "string",
                    "description": "Path inside the repo, e.g. 'docs/aws-reports/attachments/case-123.jpg'.",
                },
                "message": {
                    "type": "string",
                    "description": "Short one-line commit message.",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name. Default: main",
                    "default": "main",
                },
            },
            "required": ["local_path", "repo", "path", "message"],
        },
        handler=lambda args, ctx: _json.dumps(
            upload_local_file_to_github(
                local_path=args.get("local_path", ""),
                repo=args.get("repo", ""),
                path=args.get("path", ""),
                message=args.get("message", ""),
                branch=args.get("branch", "main"),
            ),
            indent=2,
        ),
    ),
]
