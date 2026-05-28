"""Read-only Google Docs tool for the autopilot agent.

Exposes ``read_google_doc(document_id, service_account_name=None)`` returning
the document title + flattened paragraph text. Bounded at ~64KB output so a
giant doc can't drown the model.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .google_creds import load_credentials

logger = logging.getLogger("autopilot.tools.google_docs")

DOCS_SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]
_MAX_TEXT_CHARS = 64_000


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _flatten_paragraphs(body: dict) -> str:
    parts: list[str] = []
    for element in body.get("content", []):
        para = element.get("paragraph")
        if not para:
            # tables / sectionBreaks / etc. — skip; the agent can re-query if
            # it needs structured data.
            continue
        line_parts: list[str] = []
        for el in para.get("elements", []):
            text_run = el.get("textRun")
            if text_run and "content" in text_run:
                line_parts.append(text_run["content"])
        parts.append("".join(line_parts))
    return "".join(parts)


def read_google_doc(
    document_id: str,
    service_account_name: str | None = None,
) -> str:
    if not document_id:
        return _err("document_id is required")

    creds = load_credentials(service_account_name, DOCS_SCOPES)
    if creds is None:
        return _err("credentials missing", service_account_name=service_account_name)

    try:
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:  # pragma: no cover
        return _err(f"google-api-python-client unavailable: {e}")

    try:
        service = build("docs", "v1", credentials=creds, cache_discovery=False)
        doc = service.documents().get(documentId=document_id).execute()
    except Exception as e:
        logger.warning("read_google_doc failed: %s", e)
        return _err(str(e), document_id=document_id)

    title = doc.get("title", "")
    text = _flatten_paragraphs(doc.get("body", {}))
    truncated = False
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
        truncated = True

    logger.info("read_google_doc ok: doc=%s title=%s chars=%d truncated=%s",
                document_id, title[:60], len(text), truncated)
    return json.dumps({
        "status": "ok",
        "document_id": document_id,
        "title": title,
        "text": text,
        "truncated": truncated,
    })


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="read_google_doc",
    description="Read the text content of a Google Doc (read-only). Returns title + flattened paragraph text, capped at ~64KB.",
    parameters={
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "The Google Doc ID (the long string between /d/ and /edit in the URL)."},
            "service_account_name": {"type": "string", "description": "Optional SA name (see read_google_sheet)."},
        },
        "required": ["document_id"],
    },
    handler=lambda args, ctx: read_google_doc(
        document_id=args.get("document_id", ""),
        service_account_name=args.get("service_account_name"),
    ),
)
