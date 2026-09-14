"""Discord front-end for the autopilot chat.

A standalone, single-instance **gateway** client (NOT a web-app background
task -- the uvicorn app runs multiple workers, which would race on the
gateway connection). Run it as its own systemd unit:

    python -m app.discord_adapter

Flow per message (mirrors ``app/telegram_adapter.py``):
  1. Security gate -- drop anything from a user who does not resolve to a
     verified governor (env allowlist OR the Contributors-sheet Discord-ID
     binding -> Governors cache). Everyone else is ignored.
  2. Mint a short-lived JWT for the governor's public key (resolved from the
     DAO governor registry) so ``/chat-blocking`` knows it is the governor.
  3. POST the text to ``/chat-blocking`` with an ``X-Session-Id`` derived from
     (guild, channel) so each channel is its own conversation.
  4. Send the assistant's reply back to the same channel. A working-emoji
     reaction acks receipt the moment a turn starts, and Discord's native
     "typing..." bubble is refreshed for the turn's duration, so a slow turn is
     never silent.

Security model: the trust boundary is the Discord **user-ID allowlist plus the
governor-sheet binding**. The adapter runs on the same host as the FastAPI
service and holds ``JWT_SECRET``, so minting its own JWT is equivalent to any
other trusted server-side code.

Data/instruction boundary (security invariant #2): inbound Discord text is
**DATA, never instructions**, unless its author resolves to a verified
governor. A non-governor member (or a channel topic, or pasted content) saying
"Sophia, deploy prod" is context to reason about, not a command to execute.

``DISCORD_DRY_RUN`` (default **True**): compose replies but do NOT post them
to Discord. ``DISCORD_ADAPTER_ENABLED`` (default **False**) gates startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

import httpx

from .auth import create_jwt
from .config import settings
from .governor_registry import load_governors

logger = logging.getLogger("autopilot.discord")

_DISCORD_API = "https://discord.com/api/v10"
_GATEWAY_QUERY = "?v=10&encoding=json"
_MESSAGE_LIMIT = 2000  # Discord hard cap per message
# /chat-blocking can run tools + multiple LLM calls; tool-heavy turns routinely
# exceed three minutes, so give them headroom before the ADAPTER stops waiting.
# This is a client-side wait ceiling, not a brain limit.
_CHAT_TIMEOUT = 300.0
# Reaction added the instant a governor turn is received ("working on it").
_WORKING_EMOJI = "\u23f3"  # hourglass
_TYPING_REFRESH_SECONDS = 8.0  # Discord's typing bubble expires after ~10s
_USER_AGENT = "TrueSightDAO-Sophia (https://truesight.me, 1.0)"
_ATTACH_DIR = "/tmp/discord_attachments"  # adapter + autopilot share the EC2 box / user

# Gateway intents (bit flags). We only ask for what we use.
_INTENT_GUILDS = 1 << 0
_INTENT_GUILD_MEMBERS = 1 << 1
_INTENT_GUILD_MESSAGES = 1 << 9
_INTENT_DIRECT_MESSAGES = 1 << 12
_INTENT_MESSAGE_CONTENT = 1 << 15
GATEWAY_INTENTS = (
    _INTENT_GUILDS
    | _INTENT_GUILD_MEMBERS
    | _INTENT_GUILD_MESSAGES
    | _INTENT_DIRECT_MESSAGES
    | _INTENT_MESSAGE_CONTENT
)

# Discord close codes we should NOT immediately retry (fatal config errors).
_FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}

# ── Cookie / placeholder ────────────────────────────────────────────────
_SHEET_CONTACT = "Contributors contact information"
_LEDGER_SPREADSHEET_ID = "1GE7PUq-UT6x2rBN-Q2ksogbWpgyuh2SaxJyG_uEK6PU"
COL_DISCORD_ID = 6  # Column G of the contact sheet ("Discord ID")
_BINDING_CACHE_TTL = int(os.getenv("DISCORD_BINDING_CACHE_TTL", "300"))
_binding_cache: dict[str, tuple[float, str | None]] = {}


# ── Pure helpers (unit-tested) ──────────────────────────────────────────


def parse_allowed_ids(raw: str) -> set[str]:
    """Parse 'DISCORD_ALLOWED_USER_IDS' into a set of string snowflakes.

    Discord ids are 64-bit snowflakes; they exceed JS's safe integer range, so
    they are handled as **strings** end to end (never int-coerced).
    """
    out: set[str] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(part)
    return out


def is_allowed(user_id: str | int, allowed: set[str]) -> bool:
    """True only if the allowlist is configured AND the user is on it."""
    return bool(allowed) and str(user_id) in allowed


def build_session_id(guild_id: str | int, channel_id: str | int) -> str:
    """Map a Discord (guild, channel) to a stable autopilot session id."""
    return f"dc:{guild_id}:{channel_id}"


def chunk_text(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split long text into <=limit chunks, preferring blank-line/space breaks."""
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split = window.rfind("\n\n")
        if split < limit // 2:
            split = window.rfind("\n")
        if split < limit // 2:
            split = window.rfind(" ")
        if split <= 0:
            split = limit
        chunks.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def strip_bot_mention(content: str, bot_id: str) -> str:
    """Remove a leading/embedded '<@bot_id>' or '<@!bot_id>' mention."""
    if not content:
        return content
    for token in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        content = content.replace(token, "")
    return content.strip()


def is_mention(content: str, bot_id: str) -> bool:
    """True if the message @-mentions the bot."""
    if not content or not bot_id:
        return False
    return f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content


def _extract_message_text(data: dict[str, Any]) -> str:
    """Best-effort plain text from a MESSAGE_CREATE event payload."""
    return (data.get("content") or "").strip()


def extract_attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the message's attachment descriptors (files uploaded with it).

    Discord puts uploaded files in a top-level ``attachments`` array:
    ``[{id, filename, size, url, content_type, proxy_url}, ...]``. Only entries
    that actually carry a CDN ``url`` are returned (the url is what we fetch).
    """
    atts = data.get("attachments")
    if not isinstance(atts, list):
        return []
    return [a for a in atts if isinstance(a, dict) and a.get("url")]


def attachment_names(data: dict[str, Any]) -> str:
    """Comma-joined display names for a message's attachments (for logging).

    Used on the data-only path: a non-governor's file is *named* in the session
    log but NEVER downloaded -- an attachment can never become an instruction.
    """
    return ", ".join(
        a.get("filename") or a.get("id") or "file" for a in extract_attachments(data)
    )


# Extension -> MIME fallback, for when a CDN filename has no usable suffix.
_CT_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def download_discord_file(att: dict[str, Any]) -> str | None:
    """Download one Discord attachment to a local path the brain can read.

    Discord CDN urls are **pre-signed**, so NO ``Authorization`` header is sent
    -- the bot token must never leave ``discord.com``. Returns the absolute
    path, or None on failure.
    """
    url = att.get("url")
    if not url:
        return None
    try:
        filename = att.get("filename") or ""
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            ext = _CT_EXT.get((att.get("content_type") or "").lower(), "")
        ext = ext or ".bin"
        os.makedirs(_ATTACH_DIR, exist_ok=True)
        dest = os.path.join(_ATTACH_DIR, f"{uuid.uuid4().hex}{ext}")
        with httpx.stream("GET", url, timeout=120.0) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        logger.info("downloaded Discord attachment -> %s", dest)
        return dest
    except Exception as exc:  # noqa: BLE001 -- one bad file must not kill the turn
        logger.warning("Discord attachment download failed (%s): %s", url, exc)
        return None


# ── Vault / config ──────────────────────────────────────────────────────


def get_token() -> str:
    """Read the Discord bot token from the vault (never from env/logs)."""
    from .vault import Vault

    v = Vault()
    v.initialize()
    return v.get_value("DISCORD_BOT_TOKEN")


def resolve_governor_public_key() -> str | None:
    """Find the public key of the configured governor name in the DAO registry."""
    target = (getattr(settings, "discord_governor_name", "") or "Gary Teh").strip()
    try:
        for g in load_governors().get("governors", []):
            if g.get("name", "").strip() == target and g.get("public_key"):
                return g["public_key"]
    except Exception as exc:  # noqa: BLE001 -- registry down => no chat calls
        logger.warning("Governor registry lookup failed: %s", exc)
    return None


# ── Identity binding: Discord user id -> DAO contributor ────────────────


def _fetch_discord_id_email(discord_id: str) -> str | None:
    """Look up the email bound to this Discord id in the Contributors sheet.

    Returns the email string, or None if unbound. Never raises.
    """
    try:
        from googleapiclient.discovery import build
    except Exception:  # noqa: BLE001 -- google libs optional at call time
        return None

    # Credentials come from the shared on-host loader (config/google/*.json),
    # NOT an env-var JSON blob. The old GOOGLE_SHEETS_CREDENTIALS lookup was
    # never populated on the host, so this function returned None for EVERY
    # user and the sheet-binding half of the gate was dead code (found
    # 2026-09-16).
    from .tools.google_creds import load_credentials

    credentials = load_credentials(
        None, ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    if credentials is None:
        logger.warning("Discord binding: no Google credentials resolved")
        return None
    try:
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        rows = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=_LEDGER_SPREADSHEET_ID,
                range=f"{_SHEET_CONTACT}!A:Z",
            )
            .execute()
            .get("values", [])
        )
    except Exception as exc:  # noqa: BLE001 -- degrade to unbound
        logger.warning("Discord binding sheet read failed: %s", exc)
        return None

    want = str(discord_id).strip()
    for row in rows:
        if len(row) > COL_DISCORD_ID and (row[COL_DISCORD_ID] or "").strip() == want:
            email = (row[3] or "").strip() if len(row) > 3 else ""
            return email or None
    return None


def discord_email(discord_id: str) -> str | None:
    """Cached wrapper around :func:`_fetch_discord_id_email`."""
    now = time.time()
    cached = _binding_cache.get(str(discord_id))
    if cached is not None and (now - cached[0]) < _BINDING_CACHE_TTL:
        return cached[1]
    email = _fetch_discord_id_email(str(discord_id))
    _binding_cache[str(discord_id)] = (now, email)
    return email


def _email_is_governor(email: str | None) -> bool:
    """Check an email against the Governors cache (treasury-cache/dao_members.json)."""
    if not email:
        return False
    try:
        data = load_governors()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Governor cache lookup failed: %s", exc)
        return False
    want = email.strip().lower()
    for g in data.get("governors", []):
        if (g.get("email") or "").strip().lower() == want:
            return True
    return False


def author_role(user_id: str | int, allowed: set[str]) -> str:
    """Resolve an author to 'governor', 'member', or 'guest'.

    Order:
      1. env governor allowlist (``DISCORD_ALLOWED_USER_IDS``) -> governor
      2. sheet binding (Contributors contact information, Discord-ID column)
         + Governors cache: email is a governor -> governor
      3. bound to a real contributor, or on the env member allowlist
         (``DISCORD_MEMBER_USER_IDS``) -> member
      4. otherwise -> guest

    * governor -- may instruct the bot and authorize actions.
    * member   -- a verified contributor who is NOT a governor: may converse
                  (ask / research / draft) but is **data-only**, never an
                  instruction, and carries no governor authority.
    * guest    -- unknown; observed as context only.

    Fail-closed: any error resolves to 'guest'.
    """
    uid = str(user_id)
    if is_allowed(uid, allowed):
        return "governor"
    try:
        email = discord_email(uid)
    except Exception:  # noqa: BLE001 -- never let a binding error open the gate
        return "guest"
    if _email_is_governor(email):
        return "governor"
    member_ids = parse_allowed_ids(getattr(settings, "discord_member_user_ids", ""))
    if email or is_allowed(uid, member_ids):
        return "member"
    return "guest"


# ── REST ────────────────────────────────────────────────────────────────


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {get_token()}",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }


def _api(method: str, path: str, payload: dict[str, Any] | None = None) -> dict | None:
    """Synchronous Discord REST call with 429 backoff. Returns parsed JSON or None."""
    url = f"{_DISCORD_API}{path}"
    for attempt in range(3):
        try:
            resp = httpx.request(
                method, url, headers=_headers(), json=payload, timeout=20.0
            )
            # 204 = reaction add/remove and typing (no content)
            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.content else {}
            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(
                    "Discord 429 (%s) attempt %d/3: retrying after %.1fs",
                    path,
                    attempt + 1,
                    retry_after,
                )
                time.sleep(min(retry_after + 0.5, 30))
                continue
            logger.warning(
                "Discord %s %s -> %s: %s",
                method,
                path,
                resp.status_code,
                resp.text[:200],
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Discord request failed (%s) attempt %d/3: %s", path, attempt + 1, exc
            )
            if attempt < 2:
                time.sleep(2**attempt)
    return None


def send_message(channel_id: str | int, text: str) -> list[str]:
    """Send text to a channel; returns the list of created message ids.

    Honours DISCORD_DRY_RUN: when set, the reply is logged but NOT posted.
    """
    ids: list[str] = []
    body = text or "(empty response)"
    for chunk in chunk_text(body):
        if settings.discord_dry_run:
            logger.info(
                "[DRY_RUN] would reply to channel %s (%d chars): %.120s",
                channel_id,
                len(chunk),
                chunk,
            )
            ids.append("dry-run")
            continue
        result = _api("POST", f"/channels/{channel_id}/messages", {"content": chunk})
        if result and result.get("id"):
            ids.append(result["id"])
    return ids


def add_reaction(
    channel_id: str | int, message_id: str | int, emoji: str = _WORKING_EMOJI
) -> bool:
    """React to a message as an immediate "received" ack. Best-effort.

    Returns True when the reaction was applied. Never raises; honours
    DISCORD_DRY_RUN. A tool-heavy /chat-blocking turn can run for minutes, and
    without an ack the governor cannot tell "received, working" from "dropped".
    """
    if not message_id:
        return False
    if settings.discord_dry_run:
        logger.info("[DRY_RUN] would react %s to message %s", emoji, message_id)
        return False
    token = urllib.parse.quote(emoji, safe="")
    path = f"/channels/{channel_id}/messages/{message_id}/reactions/{token}/@me"
    return _api("PUT", path) is not None


def remove_reaction(
    channel_id: str | int, message_id: str | int, emoji: str = _WORKING_EMOJI
) -> bool:
    """Remove the working ack once the turn is done. Best-effort; never raises."""
    if not message_id:
        return False
    if settings.discord_dry_run:
        logger.info("[DRY_RUN] would unreact %s on message %s", emoji, message_id)
        return False
    token = urllib.parse.quote(emoji, safe="")
    path = f"/channels/{channel_id}/messages/{message_id}/reactions/{token}/@me"
    return _api("DELETE", path) is not None


def post_typing(channel_id: str | int) -> None:
    """Trigger Discord's typing indicator in a channel (best-effort).

    Discord clears the typing state after ~10s, so this is called repeatedly
    for the duration of a turn. Failures are swallowed: a missing bubble must
    never affect the reply.
    """
    _api("POST", f"/channels/{channel_id}/typing")


class TypingIndicator:
    """Show Discord's "Sophia is typing..." bubble while a turn runs.

    Refreshes every ``_TYPING_REFRESH_SECONDS`` (the bubble expires after ~10s)
    and stops as soon as the turn ends, so the indicator clears the moment the
    reply is posted. No bubble is shown in dry-run (nothing is posted anyway).
    """

    def __init__(
        self, channel_id: str | int, interval: float = _TYPING_REFRESH_SECONDS
    ) -> None:
        self._channel_id = channel_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if settings.discord_dry_run:
            return  # nothing is posted in dry-run; no bubble to show
        self._thread = threading.Thread(target=self._run, name="dc-typing", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            post_typing(self._channel_id)
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def __enter__(self) -> "TypingIndicator":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def log_observed_message(
    message: str, session_id: str, public_key: str, sender_name: str
) -> None:
    """POST to /chat/observe -- appends to session history, no model call."""
    try:
        token = create_jwt(public_key)
        headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
        httpx.post(
            f"{settings.autopilot_chat_url.rstrip('/')}/chat/observe",
            json={"message": message, "sender_name": sender_name},
            headers=headers,
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort; never block the loop
        logger.warning("log_observed_message failed: %s", exc)


def call_chat(message: str, session_id: str, public_key: str) -> str:
    """POST to /chat-blocking as the governor; return the assistant text."""
    token = create_jwt(public_key)
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
    try:
        resp = httpx.post(
            f"{settings.autopilot_chat_url.rstrip('/')}/chat-blocking",
            json={"message": message},
            headers=headers,
            timeout=_CHAT_TIMEOUT,
        )
    except httpx.TimeoutException:
        # The brain is alive but the turn outran our wait, so the real reply is
        # dropped. Say something TRUE and actionable -- "unreachable" would be
        # misleading, and silence is worse.
        logger.warning("chat-blocking timed out after %.0fs", _CHAT_TIMEOUT)
        return (
            "\u23f3 That turn is taking unusually long "
            f"(> {int(_CHAT_TIMEOUT)}s). The brain is still working, but I "
            "stopped waiting, so the reply was dropped \u2014 resend to retry."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat-blocking request failed: %s", exc)
        return "\u26a0\ufe0f Autopilot is unreachable right now \u2014 please retry."
    if resp.status_code != 200:
        logger.warning("chat-blocking HTTP %s: %s", resp.status_code, resp.text[:300])
        return f"\u26a0\ufe0f Autopilot returned HTTP {resp.status_code}."
    try:
        return resp.json().get("response") or resp.text
    except Exception:  # noqa: BLE001
        return resp.text


# ── Message dispatch ────────────────────────────────────────────────────


def edit_message_text(channel_id: str | int, message_id: str | int, text: str) -> bool:
    """Edit a message the bot already posted (PATCH). True on success.

    Needs its own message id, so it pairs with ``send_message``. Used to keep a
    single status message updated in place during long attachment processing.
    """
    if not message_id or message_id == "dry-run":
        return False
    if settings.discord_dry_run:
        logger.info(
            "[DRY_RUN] would edit message %s in %s: %.120s",
            message_id,
            channel_id,
            text,
        )
        return False
    result = _api(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        {"content": (text or "")[:_MESSAGE_LIMIT]},
    )
    return result is not None


def _auto_process_attachment(
    local_path: str, channel_id: str | int, session_id: str
) -> str | None:
    """Auto-detect a downloaded file, extract its content, return a summary.

    Mirrors ``telegram_adapter._auto_process_attachment``: PDF (with OCR
    fallback for scans), image (OCR + EXIF/GPS), and Word (.docx) are handled.
    Returns a summary string for the LLM, or None on failure/unknown type.

    The caller puts the returned text INLINE in the dispatched message -- we do
    NOT write the transcript from here. A separate process writing the session
    file while a turn holds the history in memory clobbers it (the cross-process
    race that bricked Telegram threads 3 and 780); the turn is the single writer
    and appends under the per-session lock.
    """
    import subprocess
    import sys
    from pathlib import Path

    SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
    ext = Path(local_path).suffix.lower()
    pdf_exts = {".pdf"}
    image_exts = {
        ".jpg",
        ".jpeg",
        ".png",
        ".tiff",
        ".tif",
        ".bmp",
        ".webp",
        ".heic",
        ".heif",
    }
    docx_exts = {".docx"}

    ids = send_message(channel_id, "\U0001f4c4 Processing attachment\u2026")
    status_id = ids[0] if ids else None

    def _update_status(msg: str) -> None:
        if status_id:
            edit_message_text(channel_id, status_id, msg)

    def _run_script(script_name: str, *args: str, timeout: int = 120) -> dict:
        script_path = SCRIPTS_DIR / script_name
        if not script_path.exists():
            return {"status": "error", "message": f"Script not found: {script_path}"}
        try:
            result = subprocess.run(
                [sys.executable, str(script_path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                return {
                    "status": "error",
                    "message": f"Script exited {result.returncode}: {result.stderr[:500]}",
                }
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Script output was not valid JSON"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Script timed out"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}

    filename = Path(local_path).name

    # --- PDF -------------------------------------------------------------
    if ext in pdf_exts:
        _update_status("\U0001f4c4 Extracting PDF text\u2026")
        pdf_result = _run_script("extract_pdf_text.py", local_path)
        if pdf_result.get("status") != "success":
            _update_status(
                f"\u26a0\ufe0f PDF extraction failed: "
                f"{pdf_result.get('message', 'unknown error')}"
            )
            return None

        page_count = pdf_result.get("page_count", 0)
        total_chars = pdf_result.get("total_chars", 0)
        is_scanned = pdf_result.get("likely_scanned_pdf", False)

        pages_text = []
        for pg in pdf_result.get("pages", []):
            t = (pg.get("text") or "").strip()
            if t:
                pages_text.append(f"--- Page {pg['page']} ---\n{t}")
        extracted_text = "\n\n".join(pages_text)

        ocr_text = ""
        if is_scanned:
            _update_status("\U0001f4c4 PDF appears scanned \u2014 running OCR\u2026")
            ocr_result = _run_script("ocr_image.py", local_path, "eng")
            if ocr_result.get("status") == "success":
                ocr_text = ocr_result.get("text", "")
                extracted_text += f"\n\n--- OCR of scanned PDF ---\n{ocr_text}"

        summary = (
            f"[Attachment auto-processed: **{filename}**]\n"
            f"- Type: PDF ({page_count} page{'s' if page_count != 1 else ''}, "
            f"{total_chars} chars)\n"
        )
        if is_scanned:
            summary += (
                f"- Scanned PDF: OCR also applied ({len(ocr_text)} chars extracted)\n"
            )
        summary += f"\nExtracted content:\n```\n{extracted_text[:45000]}\n```\n"
        if len(extracted_text) > 45000:
            summary += "\n*(content truncated to 45000 chars)*\n"
        _update_status(
            f"\u2705 Extracted {page_count} page{'s' if page_count != 1 else ''} from PDF"
        )
        return summary

    # --- Image -----------------------------------------------------------
    if ext in image_exts:
        ocr_path = local_path
        converted_from_heic = False
        if ext in (".heic", ".heif"):
            _update_status("\U0001f5bc\ufe0f Converting HEIC to JPEG\u2026")
            from .tools.qr_scanner import convert_heic_to_jpg

            jpg_path = convert_heic_to_jpg(local_path)
            if not jpg_path:
                _update_status(
                    "\u26a0\ufe0f HEIC conversion failed \u2014 image not OCR-able"
                )
                return None
            ocr_path = jpg_path
            converted_from_heic = True

        _update_status("\U0001f4f8 Running OCR on image\u2026")
        ocr_result = _run_script("ocr_image.py", ocr_path, "eng")
        if ocr_result.get("status") != "success":
            _update_status(
                f"\u26a0\ufe0f OCR failed: {ocr_result.get('message', 'unknown error')}"
            )
            return None

        extracted_text = ocr_result.get("text", "")
        confidence = ocr_result.get("avg_confidence", 0)
        quality = ocr_result.get("quality", "unknown")

        summary = (
            f"[Attachment auto-processed: **{filename}**]\n"
            f"- Type: Image (OCR confidence: {confidence}%, quality: {quality})\n"
        )
        if converted_from_heic:
            summary += "- Note: HEIC converted to JPEG (EXIF/GPS preserved)\n"

        # Surface GPS when present (phone photos of farms / cacao bags).
        try:
            from .tools.qr_scanner import extract_gps_from_image

            gps = extract_gps_from_image(ocr_path)
            if gps:
                summary += (
                    f"- \U0001f4cd GPS: {gps['lat']}, {gps['lon']}"
                    + (f" (alt {gps['alt']} m)" if gps.get("alt") is not None else "")
                    + "\n"
                )
                if gps.get("timestamp"):
                    summary += f"- \U0001f550 Captured: {gps['timestamp']}\n"
        except Exception:  # noqa: BLE001 -- GPS is a bonus, never fatal
            pass

        if extracted_text:
            summary += f"\nExtracted text:\n```\n{extracted_text[:45000]}\n```\n"
        else:
            summary += "\n*(No text detected in image)*\n"
        _update_status(f"\u2705 OCR complete (confidence: {confidence}%)")
        return summary

    # --- Word (.docx) ----------------------------------------------------
    if ext in docx_exts:
        _update_status("\U0001f4c4 Extracting Word document text\u2026")
        docx_result = _run_script("extract_docx_text.py", local_path)
        if docx_result.get("status") != "success":
            _update_status(
                f"\u26a0\ufe0f Word extraction failed: "
                f"{docx_result.get('message', 'unknown error')}"
            )
            return None

        extracted_text = docx_result.get("text", "")
        paragraph_count = docx_result.get("paragraph_count", 0)
        table_count = docx_result.get("table_count", 0)
        summary = (
            f"[Attachment auto-processed: **{filename}**]\n"
            f"- Type: Word document ({paragraph_count} paragraph"
            f"{'s' if paragraph_count != 1 else ''}, {table_count} table"
            f"{'s' if table_count != 1 else ''})\n"
        )
        if extracted_text:
            summary += f"\nExtracted content:\n```\n{extracted_text[:45000]}\n```\n"
            if len(extracted_text) > 45000:
                summary += "\n*(content truncated to 45000 chars)*\n"
        else:
            summary += "\n*(No text detected in document)*\n"
        _update_status(
            f"\u2705 Extracted {paragraph_count} paragraph"
            f"{'s' if paragraph_count != 1 else ''} from Word document"
        )
        return summary

    _update_status(f"\u26a0\ufe0f Unknown file type: {ext}")
    return None


def _ingest_attachments(
    attachments: list[dict[str, Any]],
    channel_id: str | int,
    session_id: str,
    text: str,
) -> str:
    """Download + auto-process a governor's attachments; build the brain prompt.

    Extracted content rides INLINE in the dispatched message (see
    ``_auto_process_attachment`` for why we never write the transcript here).
    """
    parts = [text or "Please inspect the attached file."]
    for att in attachments:
        name = att.get("filename") or "attachment"
        local_path = download_discord_file(att)
        if not local_path:
            parts.append(
                f"\n\n\u26a0\ufe0f Couldn't download attachment **{name}** from Discord."
            )
            continue
        summary = _auto_process_attachment(local_path, channel_id, session_id)
        if summary:
            parts.append(f"\n\n{summary}")
        else:
            parts.append(
                f"\n\n[Attachment **{name}** saved at {local_path} \u2014 use "
                f"scan_qr_from_file / scan_qr_batch for QR images, extract_pdf_text "
                f"for PDFs, ocr_image for text from images, or read_local_file for "
                f"text. After processing, use append_to_transcript to persist the "
                f"extracted content.]"
            )
    return "".join(parts)


def handle_message(
    data: dict[str, Any],
    allowed: set[str],
    public_key: str | None,
    guild_id: str,
    bot_id: str,
) -> None:
    """Process one MESSAGE_CREATE event: gate -> observe/dispatch -> reply."""
    author = data.get("author") or {}
    if author.get("bot"):
        return  # never respond to bots (incl. ourselves)

    user_id = str(author.get("id") or "")
    username = author.get("global_name") or author.get("username") or user_id
    channel_id = str(data.get("channel_id") or "")
    message_id = str(data.get("id") or "")
    raw = _extract_message_text(data)
    text = strip_bot_mention(raw, bot_id)
    attachments = extract_attachments(data)
    if not text and not attachments:
        return

    session_id = build_session_id(guild_id, channel_id)
    role = author_role(user_id, allowed)

    # Data/instruction boundary: only a governor's message is an instruction.
    # A MEMBER is a verified contributor but NOT a governor -- recognised and
    # attributed (context tied to a real identity), yet still data-only: never
    # dispatched as an instruction. Enabling member *replies* requires the
    # brain to be tier-aware (a member turn minted on the governor's public key
    # would inherit governor authority), so it stays off for now -- see
    # agentic_ai_context OPEN_FOLLOWUPS.md.
    if role != "governor":
        logger.info(
            "Discord message from %s %s (%s) in %s -- logging as context only",
            role,
            username,
            user_id,
            channel_id,
        )
        if public_key:
            observed = text
            names = attachment_names(data)
            if names:
                observed = f"{observed}\n[attachments: {names}]".strip()
            log_observed_message(observed, session_id, public_key, username)
        return

    if not public_key:
        logger.warning(
            "Governor message but no public key resolved -- cannot call brain"
        )
        send_message(
            channel_id,
            "\u26a0\ufe0f I can't reach the brain: no governor public key resolved.",
        )
        return

    logger.info(
        "Discord governor message from %s (%s) in %s -- dispatching turn",
        username,
        user_id,
        channel_id,
    )
    # Ack receipt immediately (hourglass reaction) so a slow turn is never
    # silent, and show Discord's native "typing..." bubble refreshed for the
    # duration of the turn.
    add_reaction(channel_id, message_id)
    with TypingIndicator(channel_id):
        prompt = text
        if attachments:
            prompt = _ingest_attachments(attachments, channel_id, session_id, text)
        reply = call_chat(prompt, session_id, public_key)
        send_message(channel_id, reply)
    remove_reaction(channel_id, message_id)


def _handle_message_safe(
    data: dict[str, Any],
    allowed: set[str],
    public_key: str | None,
    guild_id: str,
    bot_id: str,
) -> None:
    try:
        handle_message(data, allowed, public_key, guild_id, bot_id)
    except Exception:  # noqa: BLE001 -- never let one message kill the loop
        logger.exception("handle_message crashed")


# ── Gateway ─────────────────────────────────────────────────────────────


def _gateway_url() -> str:
    data = _api("GET", "/gateway/bot")
    if not data or not data.get("url"):
        raise RuntimeError("could not fetch gateway URL from Discord")
    return data["url"] + _GATEWAY_QUERY


async def _heartbeat(ws, interval: float, seq_getter: Callable[[], int | None]) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": 1, "d": seq_getter()}))


async def _gateway_once(
    url: str,
    allowed: set[str],
    public_key: str | None,
    guild_id: str,
    bot_id: str,
    dispatch: Callable[[dict[str, Any]], None],
) -> None:
    import websockets

    async with websockets.connect(url, max_size=2**22) as ws:
        hello = json.loads(await ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000.0
        seq: dict[str, int | None] = {"n": None}
        hb = asyncio.create_task(_heartbeat(ws, interval, lambda: seq["n"]))
        await ws.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": get_token(),
                        "intents": GATEWAY_INTENTS,
                        "properties": {"os": "linux", "browser": "truesight-sophia"},
                    },
                }
            )
        )
        logger.info("Discord gateway READY handshake sent (guild=%s)", guild_id)
        try:
            async for raw in ws:
                event = json.loads(raw)
                if event.get("s") is not None:
                    seq["n"] = event["s"]
                if event.get("t") == "MESSAGE_CREATE":
                    dispatch(event["d"])
        finally:
            hb.cancel()


async def _gateway_loop(
    allowed: set[str],
    public_key: str | None,
    guild_id: str,
    bot_id: str,
    dispatch: Callable[[dict[str, Any]], None],
) -> None:
    backoff = 5.0
    while True:
        try:
            url = _gateway_url()
            await _gateway_once(url, allowed, public_key, guild_id, bot_id, dispatch)
            logger.warning("Discord gateway closed; reconnecting in %.0fs", backoff)
        except Exception as exc:  # noqa: BLE001 -- reconnect on any failure
            logger.warning(
                "Discord gateway error: %s; reconnecting in %.0fs", exc, backoff
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60.0)


def run() -> None:
    """Entry point: gate on config, start worker threads + the gateway loop."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    if not settings.discord_adapter_enabled:
        raise SystemExit(
            "DISCORD_ADAPTER_ENABLED is not set -- refusing to start the Discord adapter."
        )

    try:
        me = _api("GET", "/users/@me")
    except Exception as exc:  # noqa: BLE001
        me = None
        logger.warning("Token check failed: %s", exc)
    if not me:
        raise SystemExit("DISCORD_BOT_TOKEN missing/invalid -- cannot start adapter.")

    bot_id = str(me.get("id") or "")
    guild_id = str(settings.discord_guild_id or "").strip()
    allowed = parse_allowed_ids(settings.discord_allowed_user_ids)
    public_key = resolve_governor_public_key()

    logger.info(
        "Discord adapter starting: bot=%s guild=%s allowlist=%s governor=%s "
        "key_resolved=%s dry_run=%s",
        me.get("username"),
        guild_id or "(any)",
        sorted(allowed) or "(BOOTSTRAP -- none set)",
        getattr(settings, "discord_governor_name", "Gary Teh"),
        public_key is not None,
        settings.discord_dry_run,
    )
    if settings.discord_dry_run:
        logger.warning(
            "DISCORD_DRY_RUN=true -- replies are composed but NOT posted to Discord."
        )

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="dc-handle") as executor:

        def dispatch(payload: dict[str, Any]) -> None:
            gid = str(payload.get("guild_id") or guild_id)
            if guild_id and gid != guild_id:
                return  # ignore other guilds
            executor.submit(
                _handle_message_safe, payload, allowed, public_key, gid, bot_id
            )

        asyncio.run(_gateway_loop(allowed, public_key, guild_id, bot_id, dispatch))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
