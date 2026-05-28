"""Gmail tools for the autopilot agent — multi-account, gmail.modify scope.

Six operations exposed to the model:

- ``gmail_search(query, account=None, max_results=20)`` — list message IDs +
  snippets matching a Gmail search query.
- ``gmail_read_message(message_id, account=None, format="text")`` — fetch a
  single message: headers + plain-text body.
- ``gmail_send(to, subject, body, account=None, cc=None, bcc=None)`` — send a
  plain-text email as the authenticated mailbox.
- ``gmail_create_draft(to, subject, body, account=None, cc=None, bcc=None)`` —
  create a draft (no send).
- ``gmail_list_labels(account=None)`` — list label id/name/type for the
  mailbox.
- ``gmail_apply_label(message_id, add_labels=None, remove_labels=None,
  account=None)`` — modify labels on a single message.

Account resolution:

- Default account name is configurable via ``GMAIL_DEFAULT_ACCOUNT`` env (else
  ``"admin"``).
- Pass ``account="admin"`` or ``account="gary"`` to switch.
- Tokens are loaded from ``GMAIL_TOKENS_DIR`` (default
  ``/opt/truesight_autopilot/config/gmail``) as ``{account}_token.json``.
- Backwards-compat: if no file is found AND the account name matches
  ``GMAIL_TOKEN_JSON``'s implicit account, fall back to that env var so the
  existing ``email_poller`` keeps working before the file is shipped.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("autopilot.tools.gmail")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DEFAULT_TOKENS_DIR = "/opt/truesight_autopilot/config/gmail"
_MAX_BODY_CHARS = 64_000
_MAX_SEARCH_RESULTS = 50


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _resolve_account(account: str | None) -> str:
    return (account or os.environ.get("GMAIL_DEFAULT_ACCOUNT") or "admin").lower()


def _token_data(account: str) -> dict | None:
    """Load the OAuth token JSON for ``account`` — file first, env-var fallback."""
    tokens_dir = Path(os.environ.get("GMAIL_TOKENS_DIR", DEFAULT_TOKENS_DIR))
    candidate = tokens_dir / f"{account}_token.json"
    if candidate.is_file():
        try:
            return json.loads(candidate.read_text())
        except Exception as e:
            logger.warning("Failed to parse %s: %s", candidate, e)
    # Legacy fallback — only the implicit "admin" account.
    if account == "admin":
        env_blob = os.environ.get("GMAIL_TOKEN_JSON", "").strip()
        if env_blob:
            try:
                return json.loads(env_blob)
            except Exception as e:
                logger.warning("Failed to parse GMAIL_TOKEN_JSON: %s", e)
    return None


def _build_service(account: str | None):
    """Returns (service, error_json_or_None)."""
    name = _resolve_account(account)
    raw = _token_data(name)
    if raw is None:
        return None, _err("gmail credentials missing", account=name)
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:  # pragma: no cover
        return None, _err(f"google client libs unavailable: {e}")

    try:
        creds = Credentials(
            token=raw.get("token"),
            refresh_token=raw.get("refresh_token"),
            token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=raw.get("client_id"),
            client_secret=raw.get("client_secret"),
            scopes=raw.get("scopes") or GMAIL_SCOPES,
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return service, None
    except Exception as e:
        return None, _err(f"Gmail client init failed: {e}", account=name)


# ── search ────────────────────────────────────────────────────────────────

def gmail_search(query: str, account: str | None = None, max_results: int = 20) -> str:
    if not query:
        return _err("query is required")
    max_results = max(1, min(int(max_results or 20), _MAX_SEARCH_RESULTS))
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        message_stubs = resp.get("messages", []) or []
        out = []
        for stub in message_stubs:
            mid = stub.get("id")
            try:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ).execute()
            except Exception as e:
                logger.warning("metadata fetch failed for %s: %s", mid, e)
                continue
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append({
                "id": mid,
                "thread_id": msg.get("threadId"),
                "snippet": msg.get("snippet", ""),
                "from": headers.get("From"),
                "to": headers.get("To"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "label_ids": msg.get("labelIds", []),
            })
        logger.info("gmail_search ok: account=%s q=%.60s hits=%d",
                    _resolve_account(account), query, len(out))
        return json.dumps({
            "status": "ok",
            "account": _resolve_account(account),
            "query": query,
            "result_count": len(out),
            "results": out,
        })
    except Exception as e:
        return _err(str(e), query=query)


# ── read ──────────────────────────────────────────────────────────────────

def _walk_parts(payload: dict, parts_out: list[dict]) -> None:
    for part in payload.get("parts", []) or []:
        parts_out.append(part)
        if part.get("parts"):
            _walk_parts(part, parts_out)


def _extract_plain_text(payload: dict) -> str:
    if not payload:
        return ""
    # Single-part: body sits on the payload itself.
    if payload.get("body", {}).get("data") and not payload.get("parts"):
        try:
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    parts: list[dict] = []
    _walk_parts(payload, parts)
    # Prefer text/plain.
    for p in parts:
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            try:
                return base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace")
            except Exception:
                continue
    # Fallback to text/html stripped of tags (very crude — agent can re-parse).
    for p in parts:
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data"):
            try:
                html = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", errors="replace")
                import re
                return re.sub(r"<[^>]+>", "", html)
            except Exception:
                continue
    return ""


def gmail_read_message(message_id: str, account: str | None = None, format: str = "text") -> str:
    if not message_id:
        return _err("message_id is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full",
        ).execute()
    except Exception as e:
        return _err(str(e), message_id=message_id)

    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    body = _extract_plain_text(payload)
    truncated = len(body) > _MAX_BODY_CHARS
    if truncated:
        body = body[:_MAX_BODY_CHARS]

    logger.info("gmail_read_message ok: account=%s id=%s body_chars=%d truncated=%s",
                _resolve_account(account), message_id, len(body), truncated)
    return json.dumps({
        "status": "ok",
        "account": _resolve_account(account),
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "snippet": msg.get("snippet", ""),
        "label_ids": msg.get("labelIds", []),
        "headers": {
            "from": headers.get("From"),
            "to": headers.get("To"),
            "cc": headers.get("Cc"),
            "subject": headers.get("Subject"),
            "date": headers.get("Date"),
            "message_id": headers.get("Message-ID"),
        },
        "body": body,
        "truncated": truncated,
    })


# ── send / draft ──────────────────────────────────────────────────────────

def _build_raw_message(
    *, to: str, subject: str, body: str,
    cc: str | None = None, bcc: str | None = None,
) -> str:
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def gmail_send(
    to: str, subject: str, body: str,
    account: str | None = None,
    cc: str | None = None, bcc: str | None = None,
) -> str:
    if not to or not subject:
        return _err("to and subject are required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    raw = _build_raw_message(to=to, subject=subject, body=body or "", cc=cc, bcc=bcc)
    try:
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        return _err(str(e), to=to, subject=subject)
    logger.info("gmail_send ok: account=%s to=%s subject=%.60s id=%s",
                _resolve_account(account), to, subject, sent.get("id"))
    return json.dumps({
        "status": "ok",
        "account": _resolve_account(account),
        "id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "label_ids": sent.get("labelIds", []),
    })


def gmail_create_draft(
    to: str, subject: str, body: str,
    account: str | None = None,
    cc: str | None = None, bcc: str | None = None,
) -> str:
    if not to or not subject:
        return _err("to and subject are required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    raw = _build_raw_message(to=to, subject=subject, body=body or "", cc=cc, bcc=bcc)
    try:
        draft = service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}},
        ).execute()
    except Exception as e:
        return _err(str(e), to=to, subject=subject)
    logger.info("gmail_create_draft ok: account=%s draft_id=%s",
                _resolve_account(account), draft.get("id"))
    return json.dumps({
        "status": "ok",
        "account": _resolve_account(account),
        "draft_id": draft.get("id"),
        "message_id": (draft.get("message") or {}).get("id"),
    })


# ── labels ────────────────────────────────────────────────────────────────

def gmail_list_labels(account: str | None = None) -> str:
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        resp = service.users().labels().list(userId="me").execute()
    except Exception as e:
        return _err(str(e))
    labels = [
        {"id": l.get("id"), "name": l.get("name"), "type": l.get("type")}
        for l in resp.get("labels", [])
    ]
    return json.dumps({
        "status": "ok",
        "account": _resolve_account(account),
        "label_count": len(labels),
        "labels": labels,
    })


def gmail_apply_label(
    message_id: str,
    add_labels: Iterable[str] | None = None,
    remove_labels: Iterable[str] | None = None,
    account: str | None = None,
) -> str:
    if not message_id:
        return _err("message_id is required")
    add_ids = list(add_labels or [])
    rm_ids = list(remove_labels or [])
    if not add_ids and not rm_ids:
        return _err("at least one of add_labels / remove_labels is required")
    service, err = _build_service(account)
    if service is None:
        return err  # type: ignore[return-value]
    try:
        resp = service.users().messages().modify(
            userId="me", id=message_id,
            body={"addLabelIds": add_ids, "removeLabelIds": rm_ids},
        ).execute()
    except Exception as e:
        return _err(str(e), message_id=message_id)
    return json.dumps({
        "status": "ok",
        "account": _resolve_account(account),
        "id": resp.get("id"),
        "label_ids": resp.get("labelIds", []),
    })
