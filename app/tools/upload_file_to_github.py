"""Tool to upload a file to any TrueSightDAO GitHub repo via the Contents API."""
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


def upload_file_to_github(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> dict[str, Any]:
    """Upload a file to a TrueSightDAO GitHub repo using the Contents API.

    The content is base64-encoded before sending. If the file already exists
    on the branch, the API will reject the request — use a different branch
    or delete the file first.

    Returns a dict with 'status' ('success' or 'error') and details.
    """
    url = f"https://api.github.com/repos/TrueSightDAO/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }

    try:
        resp = httpx.put(url, headers=_github_headers(), json=payload, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "success",
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
