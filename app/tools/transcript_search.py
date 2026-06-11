"""Cross-session transcript search for attachment recall.

Lets the autopilot search past session transcripts for attachment content.
When a governor says "remember that PDF I sent last week?", this tool finds
and returns the extracted content from the transcript repo.

Transcript repo: TrueSightDAO/truesight_autopilot_transcript
Path pattern: sessions/YYYY-MM-DD/<hash>/transcript.md
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("autopilot.tools.transcript_search")

TRANSCRIPT_REPO = "truesight_autopilot_transcript"
GITHUB_API = "https://api.github.com"
_MAX_DAYS_BACK = 90
_MAX_TRANSCRIPTS = 50


def _get_github_token() -> str:
    """Get GitHub PAT from environment."""
    return os.environ.get("TRUESIGHT_DAO_AUTOPILOT", "") or os.environ.get("GITHUB_PAT", "")


def _github_request(method: str, url: str, data: dict | None = None) -> dict:
    """Make a GitHub API request."""
    import httpx

    token = _get_github_token()
    if not token:
        return {"status": "error", "message": "GitHub token not found."}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "truesight-autopilot",
    }

    try:
        with httpx.Client(timeout=30) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            elif method == "PUT":
                resp = client.put(url, headers=headers, json=data)
            else:
                return {"status": "error", "message": f"Unsupported method: {method}"}

            if resp.status_code in (200, 201):
                return resp.json()
            elif resp.status_code == 404:
                return {"status": "not_found"}
            else:
                return {"status": "error", "message": f"GitHub API error {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        return {"status": "error", "message": f"HTTP request failed: {e}"}


def _list_session_dirs(max_days: int = 30) -> list[str]:
    """List session date directories from the transcript repo.

    Returns paths like 'sessions/2026-06-07' sorted newest-first.
    """
    today = datetime.now(timezone.utc)
    dirs: list[str] = []

    for day_offset in range(max_days):
        date_str = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        path = f"sessions/{date_str}"
        result = _github_request("GET", f"{GITHUB_API}/repos/TrueSightDAO/{TRANSCRIPT_REPO}/contents/{path}")
        if isinstance(result, list):
            dirs.append(path)
        elif isinstance(result, dict) and result.get("status") == "not_found":
            continue
        else:
            continue

    return dirs


def _list_session_hashes(date_path: str) -> list[str]:
    """List session hashes within a date directory."""
    result = _github_request("GET", f"{GITHUB_API}/repos/TrueSightDAO/{TRANSCRIPT_REPO}/contents/{date_path}")
    if not isinstance(result, list):
        return []
    return [item["name"] for item in result if item["type"] == "dir"]


def _read_transcript(date_path: str, session_hash: str) -> str | None:
    """Read a session transcript file."""
    path = f"{date_path}/{session_hash}/transcript.md"
    result = _github_request("GET", f"{GITHUB_API}/repos/TrueSightDAO/{TRANSCRIPT_REPO}/contents/{path}")
    if not isinstance(result, dict) or "content" not in result:
        return None
    try:
        return base64.b64decode(result["content"]).decode("utf-8")
    except Exception:
        return None


def _find_attachment_sections(transcript: str) -> list[dict]:
    """Extract attachment sections from a transcript.

    Looks for sections starting with '## Attachment:' and extracts
    the metadata and content that follows.
    """
    sections: list[dict] = []
    # Split on attachment headers
    parts = re.split(r"^## Attachment: ", transcript, flags=re.MULTILINE)
    for part in parts[1:]:  # skip everything before first attachment
        lines = part.split("\n")
        filename = lines[0].strip() if lines else ""
        section = {"filename": filename, "lines": []}
        in_code_block = False
        code_content: list[str] = []
        for line in lines[1:]:
            if line.strip().startswith("```"):
                if in_code_block:
                    section["extracted_text"] = "\n".join(code_content)
                    code_content = []
                    in_code_block = False
                else:
                    in_code_block = True
                continue
            if in_code_block:
                code_content.append(line)
            elif line.startswith("**"):
                # Metadata line like **Type:** PDF
                section["lines"].append(line)
        if code_content and not section.get("extracted_text"):
            section["extracted_text"] = "\n".join(code_content)
        sections.append(section)
    return sections


def search_transcript(query: str, max_days_back: int = 30) -> str:
    """Search past session transcripts for attachment content matching a query.

    Args:
        query: Search term to look for in attachment filenames and extracted text.
        max_days_back: How many days back to search (default 30, max 90).

    Returns:
        JSON string with matching attachment sections and their session info.
    """
    if not query or not query.strip():
        return json.dumps({"status": "error", "reason": "query is required"})

    max_days = min(max_days_back, _MAX_DAYS_BACK)
    query_lower = query.strip().lower()

    date_dirs = _list_session_dirs(max_days)
    if not date_dirs:
        return json.dumps(
            {"status": "ok", "matches": [], "message": "No session transcripts found in the last {max_days} days."}
        )

    matches: list[dict] = []
    transcripts_checked = 0

    for date_path in date_dirs:
        session_hashes = _list_session_hashes(date_path)
        for session_hash in session_hashes:
            if transcripts_checked >= _MAX_TRANSCRIPTS:
                break
            transcript = _read_transcript(date_path, session_hash)
            if transcript is None:
                continue
            transcripts_checked += 1

            attachments = _find_attachment_sections(transcript)
            for att in attachments:
                # Check if query matches filename or extracted text
                filename_lower = att.get("filename", "").lower()
                text = att.get("extracted_text", "") or ""
                text_lower = text.lower()

                if query_lower in filename_lower or query_lower in text_lower:
                    matches.append(
                        {
                            "session_date": date_path.replace("sessions/", ""),
                            "session_hash": session_hash,
                            "filename": att.get("filename", ""),
                            "extracted_text_preview": text[:2000] + ("..." if len(text) > 2000 else ""),
                            "full_text_length": len(text),
                        }
                    )

        if transcripts_checked >= _MAX_TRANSCRIPTS:
            break

    return json.dumps(
        {
            "status": "ok",
            "query": query,
            "max_days_back": max_days,
            "transcripts_checked": transcripts_checked,
            "matches": matches,
            "match_count": len(matches),
            "message": f"Found {len(matches)} matching attachment(s) across {transcripts_checked} transcript(s)."
            if matches
            else f"No attachments matching '{query}' found in the last {max_days} days.",
        },
        indent=2,
    )


# ── capability manifest entry ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="search_transcript",
    description="Search past session transcripts for attachment content matching a query. Use this when a governor says 'remember that file I sent' or asks about something they previously attached. Searches the truesight_autopilot_transcript repo for attachment filenames and extracted text.",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search term to look for in attachment filenames and extracted text (e.g. 'cacao pricing', 'PDF from last week', 'invoice').",
            },
            "max_days_back": {
                "type": "integer",
                "description": "How many days back to search (default 30, max 90).",
                "default": 30,
            },
        },
        "required": ["query"],
    },
    handler=lambda args, ctx: search_transcript(
        query=args.get("query", ""),
        max_days_back=args.get("max_days_back", 30),
    ),
)
