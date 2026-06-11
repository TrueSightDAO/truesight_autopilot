"""Web browsing tools for the chat agent, powered by the Tavily API.

Two capabilities:
- web_search(query, ...)  -> search the live web, return ranked results (+ optional synthesized answer)
- web_extract(urls)       -> fetch and return the cleaned text content of specific URLs

The API key comes from settings.tavily_api_key (env var TAVILY_API). Tools return
JSON strings, matching the convention of the other tools in this package.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("autopilot.web_search")

_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
_TIMEOUT = 30.0
# Keep per-page content bounded so a few pages don't blow the model's context window.
_MAX_CONTENT_CHARS = 8000


def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
) -> str:
    """Search the live web via Tavily. Returns a JSON string with results."""
    if not query or not query.strip():
        return json.dumps({"status": "error", "message": "query is required"})
    if not settings.tavily_api_key:
        return json.dumps({"status": "error", "message": "TAVILY_API is not configured on the server."})

    depth = search_depth if search_depth in ("basic", "advanced") else "basic"
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query.strip(),
        "max_results": max(1, min(int(max_results or 5), 10)),
        "search_depth": depth,
        "include_answer": bool(include_answer),
    }
    try:
        resp = httpx.post(_SEARCH_URL, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Tavily search HTTP {resp.status_code}: {resp.text[:300]}",
                }
            )
        data = resp.json()
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": (r.get("content", "") or "")[:_MAX_CONTENT_CHARS],
                "score": r.get("score"),
            }
            for r in data.get("results", [])
        ]
        out: dict[str, Any] = {
            "status": "ok",
            "query": query.strip(),
            "result_count": len(results),
            "results": results,
        }
        if include_answer and data.get("answer"):
            out["answer"] = data["answer"]
        logger.info("web_search ok: query=%.80s results=%d", query, len(results))
        return json.dumps(out)
    except Exception as e:  # noqa: BLE001 — surface any failure to the model as a tool error
        logger.warning("web_search failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


def web_extract(urls: list[str] | str) -> str:
    """Fetch the cleaned text content of one or more URLs via Tavily. Returns JSON."""
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in (urls or []) if u and u.strip()]
    if not urls:
        return json.dumps({"status": "error", "message": "at least one url is required"})
    if not settings.tavily_api_key:
        return json.dumps({"status": "error", "message": "TAVILY_API is not configured on the server."})

    payload = {"api_key": settings.tavily_api_key, "urls": urls[:10]}
    try:
        resp = httpx.post(_EXTRACT_URL, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Tavily extract HTTP {resp.status_code}: {resp.text[:300]}",
                }
            )
        data = resp.json()
        results = [
            {
                "url": r.get("url", ""),
                "content": (r.get("raw_content", "") or "")[:_MAX_CONTENT_CHARS],
            }
            for r in data.get("results", [])
        ]
        failed = data.get("failed_results", [])
        logger.info("web_extract ok: urls=%d extracted=%d failed=%d", len(urls), len(results), len(failed))
        return json.dumps(
            {
                "status": "ok",
                "extracted_count": len(results),
                "results": results,
                "failed": [f.get("url", "") for f in failed] if failed else [],
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("web_extract failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="web_search",
        description="Search the live, public web (via Tavily) for current information not in the DAO context or repos — news, docs, prices, people, external facts. Returns ranked results with snippets and an optional synthesized answer. Use web_extract afterward to read a specific result in full.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {"type": "integer", "description": "Number of results (1-10).", "default": 5},
                "search_depth": {
                    "type": "string",
                    "description": "'basic' (fast) or 'advanced' (deeper).",
                    "enum": ["basic", "advanced"],
                    "default": "basic",
                },
                "include_answer": {"type": "boolean", "description": "Include a synthesized answer.", "default": True},
            },
            "required": ["query"],
        },
        handler=lambda args, ctx: web_search(
            query=args.get("query", ""),
            max_results=args.get("max_results", 5),
            search_depth=args.get("search_depth", "basic"),
            include_answer=args.get("include_answer", True),
        ),
    ),
    ToolSpec(
        name="web_extract",
        description="Fetch and return the cleaned full-text content of one or more specific web page URLs (via Tavily). Use after web_search to read a promising result in depth, or when the user gives you a URL to read.",
        parameters={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of page URLs to read (max 10).",
                },
            },
            "required": ["urls"],
        },
        handler=lambda args, ctx: web_extract(urls=args.get("urls", [])),
    ),
]
