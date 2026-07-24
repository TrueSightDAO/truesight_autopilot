"""
Follow-up probes — check conditions for durable follow-ups.

Each probe takes a follow-up dict and a datetime, and returns
{"struck": bool, "evidence": str}.

Probes never throw exceptions — network errors return not-struck.

"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("autopilot.followups.probes")


# ── elapsed_days probe ───────────────────────────────────────────────────


def get_escalate_after_days(followup: dict[str, Any], default: int = 1) -> int:
    """Read `schedule.escalate_after_days` — the canonical location per the
    schema documented in followups.py's module docstring — falling back to
    `condition.escalate_after_days` for entries authored the way real
    OPEN_FOLLOWUPS.md examples have twice mistakenly nested it
    (chocolate-subscription-phase2, warmup-conversion-30day-readout, both
    predating this fix). Without this fallback, a misplaced key silently
    falls back to the hardcoded default of 1 day — which strikes almost
    immediately instead of after the intended escalation window, and (before
    the 2026-07-24 created_at fix) never surfaced because every follow-up
    failed earlier in the pipeline anyway. Root-caused 2026-07-24."""
    schedule = followup.get("schedule", {}) or {}
    if "escalate_after_days" in schedule:
        return schedule["escalate_after_days"]
    condition = followup.get("condition", {}) or {}
    return condition.get("escalate_after_days", default)


def elapsed_days(
    followup: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """
    Check if escalate_after_days has passed since created_at.

    Returns struck=True when the escalation day is reached or exceeded.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    created_at_str = followup.get("created_at", "")
    if not created_at_str:
        return {"struck": False, "evidence": "No created_at date"}

    escalate_after = get_escalate_after_days(followup)

    try:
        # created_at is YYYY-MM-DD
        created = datetime.fromisoformat(created_at_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"struck": False, "evidence": f"Invalid created_at: {created_at_str}"}

    elapsed = (now - created).total_seconds() / 86400
    struck = elapsed >= escalate_after

    return {
        "struck": struck,
        "evidence": (
            f"{elapsed:.1f} days elapsed since {created_at_str}, "
            f"threshold is {escalate_after} day(s)"
        ),
    }


# ── gmail_reply probe ────────────────────────────────────────────────────


def _build_gmail_service():
    """Build a Gmail API service instance.

    Reuses the same credential pattern as email_poller.EmailPoller.
    Returns None if Gmail is not configured.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        from app.config import settings

        token_json = settings.gmail_token_json
        if not token_json:
            logger.warning("GMAIL_TOKEN_JSON not set — gmail_reply probe disabled")
            return None

        creds_data = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_data)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error("Failed to build Gmail service for probe: %s", e)
        return None


def gmail_reply(
    followup: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    """
    Check if a reply has arrived from a named sender since created_at.

    Reads condition.from and optional condition.subject_contains from
    the follow-up definition. Queries Gmail for messages matching the
    sender since the follow-up's created_at date.

    Returns struck=True when at least one matching message is found.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    condition = followup.get("condition", {})
    sender = condition.get("from", "")
    if not sender:
        return {"struck": False, "evidence": "No 'from' address in condition"}

    created_at_str = followup.get("created_at", "")
    if not created_at_str:
        return {"struck": False, "evidence": "No created_at date"}

    # Build Gmail query
    # Search for messages FROM the sender, after the created_at date
    query = f"from:{{{sender}}} after:{created_at_str}"

    subject_contains = condition.get("subject_contains", "")
    if subject_contains:
        query += f" subject:{subject_contains}"

    gmail = _build_gmail_service()
    if gmail is None:
        return {"struck": False, "evidence": "Gmail service not available"}

    try:
        results = (
            gmail.users().messages().list(userId="me", q=query, maxResults=5).execute()
        )
        messages = results.get("messages", [])

        if not messages:
            return {
                "struck": False,
                "evidence": f"No messages from {sender} since {created_at_str}",
            }

        # Fetch the most recent message to get subject/date
        latest = (
            gmail.users()
            .messages()
            .get(userId="me", id=messages[0]["id"], format="metadata")
            .execute()
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in latest.get("payload", {}).get("headers", [])
        }

        return {
            "struck": True,
            "evidence": (
                f"Found {len(messages)} message(s) from {sender} since {created_at_str}. "
                f"Latest: '{headers.get('subject', 'N/A')}' on {headers.get('date', 'N/A')}"
            ),
        }
    except Exception as e:
        logger.error("Gmail reply probe failed: %s", e)
        return {"struck": False, "evidence": f"Gmail query error: {e}"}


# ── probe registry ───────────────────────────────────────────────────────


PROBE_REGISTRY: dict[str, Any] = {
    "elapsed_days": elapsed_days,
    "gmail_reply": gmail_reply,
}


def run_probe(followup: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """
    Run the appropriate probe for a follow-up based on its condition.kind.

    Returns {"struck": bool, "evidence": str}.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    condition = followup.get("condition", {})
    kind = condition.get("kind", "")

    probe_fn = PROBE_REGISTRY.get(kind)
    if probe_fn is None:
        return {
            "struck": False,
            "evidence": f"Unknown probe kind: {kind}",
        }

    try:
        return probe_fn(followup, now)
    except Exception as e:
        logger.exception("Probe %s failed: %s", kind, e)
        return {"struck": False, "evidence": f"Probe error: {e}"}
