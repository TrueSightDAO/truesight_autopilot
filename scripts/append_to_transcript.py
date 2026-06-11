#!/usr/bin/env python3
"""Append structured attachment content to a session transcript.

Reads the current session transcript from truesight_autopilot_transcript,
appends a structured section with extracted content, and commits/pushes.

Usage:
    python3 scripts/append_to_transcript.py \
        --session-id <hash> \
        --content <extracted_text> \
        --filename <original_filename> \
        --type <PDF|Image> \
        [--ocr-text <ocr_result>] \
        [--grok-description <grok_description>] \
        [--chat-id <telegram_chat_id>] \
        [--thread-id <telegram_thread_id>]

Output:
    JSON with status and transcript URL.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("append_to_transcript")

TRANSCRIPT_REPO = "truesight_autopilot_transcript"
GITHUB_API = "https://api.github.com"


def get_github_token() -> str:
    """Get GitHub PAT from environment."""
    token = os.environ.get("TRUESIGHT_DAO_AUTOPILOT", "") or os.environ.get("GITHUB_PAT", "")
    if not token:
        # Try reading from .env
        env_paths = [
            Path("/opt/truesight_autopilot/.env"),
            Path(".env"),
        ]
        for env_path in env_paths:
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("TRUESIGHT_DAO_AUTOPILOT="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    return token


def github_request(method: str, url: str, data: dict | None = None) -> dict:
    """Make a GitHub API request."""
    import httpx

    token = get_github_token()
    if not token:
        return {"status": "error", "message": "GitHub token not found. Set TRUESIGHT_DAO_AUTOPILOT env var."}

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
            elif resp.status_code == 422:
                return {"status": "error", "message": f"GitHub validation error: {resp.text[:500]}"}
            else:
                return {"status": "error", "message": f"GitHub API error {resp.status_code}: {resp.text[:500]}"}
    except Exception as e:
        return {"status": "error", "message": f"HTTP request failed: {e}"}


def get_transcript_path(session_id: str) -> str:
    """Build the transcript file path for a session."""
    import hashlib

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sid_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
    return f"sessions/{today}/{sid_hash}/transcript.md"


def append_to_transcript(
    session_id: str,
    content: str,
    filename: str,
    file_type: str,
    ocr_text: str = "",
    grok_description: str = "",
    chat_id: str = "",
    thread_id: str = "",
) -> dict:
    """Append attachment content to the session transcript.

    Args:
        session_id: Session hash/ID.
        content: Main extracted text content.
        filename: Original filename of the attachment.
        file_type: "PDF" or "Image".
        ocr_text: OCR-extracted text (for images).
        grok_description: Grok vision description (for images).
        chat_id: Telegram chat ID (optional).
        thread_id: Telegram thread/topic ID (optional).

    Returns:
        Dict with status and transcript URL.
    """
    path = get_transcript_path(session_id)
    url = f"{GITHUB_API}/repos/TrueSightDAO/{TRANSCRIPT_REPO}/contents/{path}"

    # Try to read existing transcript
    existing = github_request("GET", url)
    existing_content = ""
    sha = None

    if existing.get("status") != "not_found" and "content" in existing:
        try:
            existing_content = base64.b64decode(existing["content"]).decode("utf-8")
            sha = existing.get("sha")
        except Exception:
            pass

    # Build the attachment section
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    attachment_section = f"\n\n---\n\n## Attachment: {filename}\n\n"
    attachment_section += "| Field | Value |\n"
    attachment_section += "|-------|-------|\n"
    attachment_section += f"| **Type** | {file_type} |\n"
    attachment_section += f"| **Filename** | {filename} |\n"
    attachment_section += f"| **Received** | {timestamp} |\n"
    if chat_id:
        attachment_section += f"| **Telegram Chat ID** | {chat_id} |\n"
    if thread_id:
        attachment_section += f"| **Telegram Thread ID** | {thread_id} |\n"

    if file_type == "Image" and grok_description:
        attachment_section += f"| **Grok Description** | {grok_description[:200]} |\n"

    attachment_section += "\n"

    if content:
        attachment_section += "### Extracted Text\n\n"
        attachment_section += f"```\n{content[:10000]}\n```\n"
        if len(content) > 10000:
            attachment_section += "\n*(Text truncated to 10,000 characters)*\n"

    if file_type == "Image" and ocr_text:
        attachment_section += "\n### OCR Result\n\n"
        attachment_section += f"```\n{ocr_text[:5000]}\n```\n"
        if len(ocr_text) > 5000:
            attachment_section += "\n*(OCR text truncated)*\n"

    if file_type == "Image" and grok_description:
        attachment_section += "\n### Grok Vision\n\n"
        attachment_section += f"{grok_description[:2000]}\n"

    new_content = existing_content + attachment_section
    encoded = base64.b64encode(new_content.encode("utf-8")).decode("ascii")

    # Commit
    commit_data = {
        "message": f"[autopilot] Attachment: {filename} ({file_type}) for session {session_id[:12]}",
        "content": encoded,
        "branch": "main",
    }
    if sha:
        commit_data["sha"] = sha

    result = github_request("PUT", url, commit_data)

    if "content" in result:
        blob_url = f"https://github.com/TrueSightDAO/{TRANSCRIPT_REPO}/blob/main/{path}"
        return {
            "status": "success",
            "transcript_url": blob_url,
            "session_id": session_id[:12],
            "message": f"Attachment '{filename}' appended to transcript.",
        }
    else:
        return {
            "status": "error",
            "message": result.get("message", "Failed to write transcript"),
        }


def main():
    parser = argparse.ArgumentParser(description="Append attachment content to session transcript")
    parser.add_argument("--session-id", required=True, help="Session hash/ID")
    parser.add_argument("--content", required=True, help="Extracted text content")
    parser.add_argument("--filename", required=True, help="Original filename")
    parser.add_argument("--type", required=True, choices=["PDF", "Image"], help="File type")
    parser.add_argument("--ocr-text", default="", help="OCR extracted text (for images)")
    parser.add_argument("--grok-description", default="", help="Grok vision description (for images)")

    args = parser.parse_args()
    result = append_to_transcript(
        session_id=args.session_id,
        content=args.content,
        filename=args.filename,
        file_type=args.type,
        ocr_text=args.ocr_text,
        grok_description=args.grok_description,
        chat_id=args.chat_id,
        thread_id=args.thread_id,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
