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
     reaction acks receipt the moment a turn starts, so a slow turn is never
     silent.

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
import time
import urllib.parse
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
            if resp.status_code in (200, 201, 204):  # 204 = reaction add/remove
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
    if not text:
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
            log_observed_message(text, session_id, public_key, username)
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
    # Ack receipt immediately so a slow turn never looks like silence.
    add_reaction(channel_id, message_id)
    reply = call_chat(text, session_id, public_key)
    remove_reaction(channel_id, message_id)
    send_message(channel_id, reply)


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
