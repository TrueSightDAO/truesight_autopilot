"""Daily briefing endpoint for the Morning Oracle Standup.

When a governor casts their morning oracle reading, the oracle fires a signed
trigger to this endpoint. Sophia verifies the key, dedups per governor per day,
composes a personalized agenda from live DAO sources, and posts it in Telegram
#General — so when the governor arrives, the day's action items are already
waiting.

Generated-by: Sophia (TrueSight Autopilot)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .auth import verify_payload
from .config import settings
from .github_client import GitHubClient
from .governor_registry import load_governors

logger = logging.getLogger("autopilot.daily_briefing")

# ── Constants ──────────────────────────────────────────────────────────────

_TELEGRAM_API = "https://api.telegram.org"
_GENERAL_CHAT_ID = -1003919341801  # TrueSight DAO Ops #General (no thread_id)

# Dedup store: file per governor, keyed by date
_DEDUP_DIR = Path(settings.session_log_dir) / "daily_briefing_dedup"

# GitHub paths for live agenda sources
_AGENTIC_CONTEXT_REPO = "agentic_ai_context"
_HANDOFFS_PATH = "SOPHIA_HANDOFFS.md"
_FOLLOWUPS_PATH = "OPEN_FOLLOWUPS.md"


# ── Helpers ────────────────────────────────────────────────────────────────


def _gov_name_for_key(public_key_b64: str) -> str | None:
    """Look up governor name from public key. Returns name or None."""
    data = load_governors()
    for g in data.get("governors", []):
        if g.get("public_key") == public_key_b64:
            return g.get("name")
    return None


def _dedup_key(public_key: str) -> str:
    """Build a dedup key: sha256(public_key)[:12]:YYYY-MM-DD."""
    h = hashlib.sha256(public_key.encode()).hexdigest()[:12]
    return f"{h}:{date.today().isoformat()}"


def _check_dedup(public_key: str) -> bool:
    """Check if this governor already received a briefing today.
    Returns True if already briefed (should skip).
    """
    key = _dedup_key(public_key)
    _DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    dedup_file = _DEDUP_DIR / f"{key}.json"
    if dedup_file.exists():
        try:
            data = json.loads(dedup_file.read_text())
            if data.get("briefed_today"):
                logger.info("Dedup: governor %s already briefed today", key)
                return True
        except Exception:
            pass
    return False


def _mark_dedup(public_key: str, governor_name: str, reading_data: dict | None = None) -> None:
    """Mark this governor as briefed today."""
    key = _dedup_key(public_key)
    _DEDUP_DIR.mkdir(parents=True, exist_ok=True)
    dedup_file = _DEDUP_DIR / f"{key}.json"
    dedup_file.write_text(
        json.dumps(
            {
                "briefed_today": True,
                "governor": governor_name,
                "public_key_prefix": public_key[:20],
                "briefed_at_utc": datetime.now(timezone.utc).isoformat(),
                "reading": reading_data,
            },
            indent=2,
        )
    )
    logger.info("Dedup: marked governor %s as briefed today", key)


# ── Agenda sources ─────────────────────────────────────────────────────────


def _fetch_handoffs() -> str:
    """Fetch SOPHIA_HANDOFFS.md and extract active/GO-ready rows."""
    try:
        gh = GitHubClient()
        result = gh.read_file(_AGENTIC_CONTEXT_REPO, _HANDOFFS_PATH)
        if result.get("type") != "file":
            return "(handoffs unavailable)"
        content = result["content"]

        # Extract registry table rows with status "active" or "GO-ready"
        lines = content.split("\n")
        in_table = False
        active_handoffs: list[str] = []
        for line in lines:
            if line.startswith("| Date |"):
                in_table = True
                continue
            if in_table and line.startswith("|---"):
                continue
            if in_table and line.startswith("|"):
                # Parse: | date | handoff | plan file | topic | thread_id | session_id | status |
                cols = [c.strip() for c in line.split("|")]
                if len(cols) >= 7:
                    status = cols[6].lower()
                    if status in ("active", "go-ready"):
                        handoff_name = cols[2]
                        plan_file = cols[3]
                        topic_link = cols[4]
                        active_handoffs.append(f"  • {handoff_name} — {plan_file} ({topic_link})")
            elif in_table and not line.startswith("|"):
                in_table = False

        if active_handoffs:
            return "\n".join(active_handoffs)
        return "(no active handoffs)"
    except Exception as e:
        logger.warning("Failed to fetch handoffs: %s", e)
        return "(handoffs unavailable)"


def _fetch_open_prs() -> str:
    """Fetch open PRs across DAO repos needing review/merge."""
    try:
        gh = GitHubClient()
        # Check key repos for open PRs
        repos_to_check = [
            "truesight_autopilot",
            "dao_protocol",
            "oracle",
            "dapp_beta",
            "tokenomics",
            "go_to_market",
            "market_research",
            "agroverse_shop_beta",
            "truesight_me_beta",
            "capoeira",
        ]
        all_prs: list[dict] = []
        for repo in repos_to_check:
            try:
                prs = gh.list_prs(repo, state="open", limit=5)
                if isinstance(prs, list):
                    for pr in prs:
                        all_prs.append(
                            {
                                "repo": repo,
                                "number": pr.get("number", "?"),
                                "title": pr.get("title", "")[:80],
                                "url": pr.get("url", ""),
                                "created_at": pr.get("created_at", ""),
                            }
                        )
            except Exception:
                pass

        if not all_prs:
            return "(no open PRs)"

        # Sort by created_at descending, take top 10
        all_prs.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        lines = []
        for pr in all_prs[:10]:
            lines.append(f"  • {pr['repo']}#{pr['number']}: {pr['title']}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch open PRs: %s", e)
        return "(open PRs unavailable)"


def _fetch_due_followups() -> str:
    """Fetch OPEN_FOLLOWUPS.md and extract pending items."""
    try:
        gh = GitHubClient()
        result = gh.read_file(_AGENTIC_CONTEXT_REPO, _FOLLOWUPS_PATH)
        if result.get("type") != "file":
            return "(follow-ups unavailable)"
        content = result["content"]

        # Extract the ## Pending section
        lines = content.split("\n")
        in_pending = False
        pending_items: list[str] = []
        for line in lines:
            if line.strip().startswith("## Pending"):
                in_pending = True
                continue
            if in_pending and line.strip().startswith("## "):
                break
            if in_pending and line.strip().startswith("### "):
                title = line.strip().lstrip("# ").strip()
                pending_items.append(f"  • {title}")

        if pending_items:
            return "\n".join(pending_items[:8])  # top 8
        return "(no pending follow-ups)"
    except Exception as e:
        logger.warning("Failed to fetch follow-ups: %s", e)
        return "(follow-ups unavailable)"


def _fetch_in_flight_status() -> str:
    """Check what's currently in-flight by reading recent context."""
    try:
        gh = GitHubClient()
        # Read CONTEXT_UPDATES.md for recent activity
        result = gh.read_file(_AGENTIC_CONTEXT_REPO, "CONTEXT_UPDATES.md")
        if result.get("type") == "file":
            content = result["content"]
            lines = content.split("\n")
            # Get last 5 non-empty lines
            recent = [ln for ln in lines if ln.strip()][-5:]
            if recent:
                return "\n".join(f"  • {ln.strip().lstrip('- ')}" for ln in recent)
        return "(no recent activity logged)"
    except Exception as e:
        logger.warning("Failed to fetch in-flight status: %s", e)
        return "(in-flight status unavailable)"


# ── Compose agenda ────────────────────────────────────────────────────────


def _compose_agenda(
    governor_name: str,
    reading_data: dict | None = None,
) -> str:
    """Compose a personalized daily briefing agenda from live sources."""
    handoffs = _fetch_handoffs()
    open_prs = _fetch_open_prs()
    followups = _fetch_due_followups()
    inflight = _fetch_in_flight_status()

    # Hexagram framing
    hexagram_line = ""
    if reading_data:
        primary = reading_data.get("primary_hexagram", {})
        primary_number = primary.get("number", "")
        primary_name = primary.get("name", "")
        if primary_number and primary_name:
            hexagram_line = f"\n🌅 Today's hexagram: **{primary_number} — {primary_name}**"

    lines = [
        f"☀️ **Good morning, {governor_name}!**",
        hexagram_line,
        "",
        "📋 **Morning Briefing**",
        "",
        "**🔔 Parked handoffs awaiting your attention:**",
        handoffs,
        "",
        "**🔧 Open PRs needing review/merge:**",
        open_prs,
        "",
        "**📌 Due follow-ups:**",
        followups,
        "",
        "**🔄 In-flight status:**",
        inflight,
        "",
        "---",
        'Reply with **"go for it"** on any item and I\'ll execute it. '
        "Or cast the oracle at https://oracle.truesight.me for today's direction.",
        "",
        f"<i>Generated by Sophia (TrueSight Autopilot) at {datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>",
    ]
    return "\n".join(ln for ln in lines if ln)


# ── Telegram post ──────────────────────────────────────────────────────────


def _post_to_telegram(text: str) -> bool:
    """Post the briefing to Telegram #General.
    Returns True on success.
    """
    if not settings.telegram_bot_api_key:
        logger.warning("TELEGRAM_BOT_API_KEY not set — cannot post briefing")
        return False

    # Telegram HTML formatting
    # Escape &, <, > in text but preserve <b>, <i>, <code>, <pre>, <a> tags
    def _escape_html(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Simple markdown-like to Telegram HTML conversion
    html_lines = []
    for line in text.split("\n"):
        if line.startswith("**") and "**" in line[2:]:
            # Bold line
            inner = line.strip("* ")
            html_lines.append(f"<b>{_escape_html(inner)}</b>")
        elif line.startswith("  • "):
            html_lines.append(f"  • {_escape_html(line[4:])}")
        elif line.startswith("<i>") and line.endswith("</i>"):
            html_lines.append(line)  # already HTML
        elif line.startswith("---"):
            html_lines.append("─" * 20)
        else:
            html_lines.append(_escape_html(line))

    html_text = "\n".join(html_lines)

    payload = {
        "chat_id": _GENERAL_CHAT_ID,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        resp = httpx.post(
            f"{_TELEGRAM_API}/bot{settings.telegram_bot_api_key}/sendMessage",
            json=payload,
            timeout=20.0,
        )
        if resp.status_code == 200:
            logger.info("Briefing posted to #General (chat %s)", _GENERAL_CHAT_ID)
            return True
        else:
            logger.warning("Failed to post briefing: HTTP %s: %s", resp.status_code, resp.text[:200])
            # Fallback: plain text
            fallback = {"chat_id": _GENERAL_CHAT_ID, "text": text, "disable_web_page_preview": True}
            try:
                resp2 = httpx.post(
                    f"{_TELEGRAM_API}/bot{settings.telegram_bot_api_key}/sendMessage",
                    json=fallback,
                    timeout=20.0,
                )
                return resp2.status_code == 200
            except Exception:
                return False
    except Exception as e:
        logger.warning("Failed to post briefing: %s", e)
        return False


# ── Main handler ───────────────────────────────────────────────────────────


async def handle_daily_briefing(payload: dict, signature: str, public_key: str) -> dict:
    """Handle a daily briefing request.

    Steps:
    1. Verify the signature maps to a governor
    2. Dedup per governor per day
    3. Compose agenda from live sources
    4. Post to Telegram #General
    5. Return result

    Returns a dict with status and message.
    """
    # 1. Verify signature
    try:
        verify_payload(payload, signature, public_key)
    except Exception as e:
        logger.warning("Briefing: signature verification failed: %s", e)
        return {"ok": False, "error": "Signature verification failed"}

    # 2. Resolve governor
    governor_name = _gov_name_for_key(public_key)
    if not governor_name:
        logger.warning("Briefing: public key does not map to a governor")
        return {"ok": False, "error": "Not a governor"}

    # 3. Dedup
    if _check_dedup(public_key):
        logger.info("Briefing: governor %s already briefed today — skipping", governor_name)
        return {"ok": True, "message": "Already briefed today", "dedup": True}

    # 4. Extract reading data from payload
    reading_data = payload.get("reading", {})
    if not reading_data:
        # Try nested under the payload itself
        reading_data = {
            "primary_hexagram": payload.get("primary_hexagram"),
            "timestamp_utc": payload.get("timestamp_utc"),
        }

    # 5. Compose agenda
    agenda = _compose_agenda(governor_name, reading_data)

    # 6. Post to Telegram #General
    posted = _post_to_telegram(agenda)

    # 7. Mark dedup
    _mark_dedup(public_key, governor_name, reading_data)

    if posted:
        logger.info("Briefing delivered for governor %s", governor_name)
        return {"ok": True, "message": "Briefing posted to #General", "governor": governor_name}
    else:
        logger.warning("Briefing composed but Telegram post failed for %s", governor_name)
        return {"ok": False, "error": "Telegram post failed", "governor": governor_name}
