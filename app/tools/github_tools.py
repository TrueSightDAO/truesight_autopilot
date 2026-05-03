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
                    {"name": item.get("name"), "type": item.get("type"), "path": item.get("path")}
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


def search_codebase(repo: str, query: str) -> dict[str, Any]:
    url = "https://api.github.com/search/code"
    params = {"q": f"repo:TrueSightDAO/{repo} {query}"}

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
