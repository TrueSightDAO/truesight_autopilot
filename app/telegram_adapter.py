"""Telegram front-end for the autopilot chat.

A standalone, single-instance long-poller (NOT a web-app background task — the
uvicorn app runs multiple workers, which would race on getUpdates). Run it as
its own systemd unit:  python -m app.telegram_adapter

Flow per message:
  1. Drop anything from a Telegram user not on the allowlist (the security gate).
  2. Mint a short-lived JWT for the configured governor's public key (resolved
     from the DAO governor registry) so /chat-blocking knows it's "Gary Teh".
  3. POST the text to /chat-blocking with an X-Session-Id derived from the
     Telegram chat + topic (so each topic is its own conversation).
  4. Send the assistant's reply back to the same chat/topic.

Voice message flow:
  1. Transcribe voice note via faster-whisper (local, free)
  2. Detect language from transcription
  3. Send transcribed text to /chat (SSE) for processing
  4. Synthesize assistant's response as MP3 via edge-tts (local, free)
  5. Send voice reply + URL follow-up text if URLs are present

Security model: the trust boundary is the Telegram user-ID allowlist. The
adapter runs on the same host as the FastAPI service and holds JWT_SECRET, so
minting its own JWT is equivalent to any other trusted server-side code.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any

import httpx

from .auth import create_jwt
from .config import settings
from .governor_registry import load_governors
from .voice import transcribe_voice
from .voice_output import detect_language, synthesize_voice

logger = logging.getLogger("autopilot.telegram")

_TELEGRAM_API = "https://api.telegram.org"
_MESSAGE_LIMIT = 4096  # Telegram hard cap per message
_CHAT_TIMEOUT = 180.0  # /chat-blocking can run tools + multiple LLM calls
_POLL_TIMEOUT = 30  # long-poll seconds
_TYPING_INTERVAL = 4.0  # Telegram's "typing…" lasts ~5s; refresh under that
_ATTACH_DIR = "/tmp/tg_attachments"  # adapter + autopilot share the EC2 box / user

# Per-thread dispatch locks. The handler pool (max_workers=10) runs messages
# concurrently, so without this two messages for the SAME topic would dispatch
# overlapping turns and race on the transcript. One lock per (chat, thread)
# serializes dispatch within a topic (one executor per thread, queued in arrival
# order) while DIFFERENT topics stay parallel. Attachment prep (download + OCR /
# extraction) runs BEFORE the lock, so it still parallelizes — only the turn is
# serialized. See agentic_ai_context/SOPHIA_THREAD_CONCURRENCY_PLAN.md (PR2).
_thread_dispatch_locks: dict[str, threading.Lock] = {}
_thread_dispatch_guard = threading.Lock()


def _thread_dispatch_lock(chat_id: int, thread_id: int | None) -> threading.Lock:
    key = f"{chat_id}:{thread_id or 0}"
    with _thread_dispatch_guard:
        lock = _thread_dispatch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_dispatch_locks[key] = lock
        return lock


# Short status-check phrases that, when sent into a BUSY topic, should be
# answered immediately from the live-progress record (lock-bypassing) rather
# than queued as a new instruction.
_PROGRESS_QUERY_RE = re.compile(
    r"\b(progress|status|update|how('?s| is| are)\s+(it|things|you|we)|"
    r"how'?s\s+it\s+going|where\s+(are|r)\s+(you|u|we)|"
    r"(are|r)\s+(you|u|we)\s+(done|finished|there)|done\s+yet|finished\s+yet|"
    r"what'?s\s+(happening|going\s+on)|any\s+update)\b",
    re.IGNORECASE,
)


def _is_progress_query(text: str | None) -> bool:
    """True only when *text* is a SHORT status check about the in-flight turn, so
    it can be answered immediately from live progress instead of queued. Biased
    hard toward False — a real instruction must never be misread as a status
    query (which would silently drop it). Long messages are always treated as
    instructions."""
    if not text:
        return False
    t = text.strip()
    if len(t) > 80:  # real instructions are longer than a status ping
        return False
    return bool(_PROGRESS_QUERY_RE.search(t))


def _fetch_progress_snapshot(session_id: str, public_key: str) -> str | None:
    """Fetch the running turn's live-progress snapshot from the brain over HTTP.

    The Telegram adapter is a SEPARATE process from the FastAPI brain, so the
    in-process ``_live_progress`` dict (populated by the running turn inside the
    brain) is ALWAYS empty in this process — reading it directly is the bug that
    made every mid-turn ack generic (fixed 2026-06-17). We must cross the process
    boundary via ``/chat/progress`` (which now accepts the Bearer JWT and keys the
    session the same way ``/chat`` does). Returns the snapshot, or None when
    nothing is running or the call fails (caller then sends a plain ack)."""
    try:
        token = create_jwt(public_key)
        resp = httpx.get(
            f"{settings.autopilot_chat_url.rstrip('/')}/chat/progress",
            headers={"Authorization": f"Bearer {token}", "X-Session-Id": session_id},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("running") and data.get("snapshot"):
                return data["snapshot"]
    except Exception:  # noqa: BLE001 — progress is best-effort; never break the ack
        pass
    return None


def _ack_queued_if_busy(
    chat_id: int,
    thread_id: int | None,
    lock: threading.Lock,
    session_id: str | None = None,
    public_key: str | None = None,
) -> None:
    """If a turn is already running in this topic, immediately tell the governor
    their new message was received and queued — it will be handled after the
    current turn finishes. Without this, a message sent mid-task just blocks
    silently on the lock, so it looks dropped. (The new message's own turn still
    runs in arrival order once the lock frees.)

    If *session_id* and *public_key* are provided, includes a live-progress
    snapshot from the running turn (current tool, round, elapsed, done-so-far)
    so the ack is informative rather than blind. The snapshot is fetched over
    HTTP because the brain runs in a different process (see
    ``_fetch_progress_snapshot``)."""
    if lock.locked():
        progress = ""
        if session_id and public_key:
            snap = _fetch_progress_snapshot(session_id, public_key)
            if snap:
                progress = f"\n\nRight now: {snap}"
        send_message(
            chat_id,
            "📥 Got it — I'm still finishing the previous task in this topic. "
            "I've added this to the queue and will get to it right after." + progress,
            thread_id,
        )


# ── Pure helpers (unit-tested) ─────────────────────────────────────────────


def parse_allowed_ids(raw: str) -> set[int]:
    """Parse 'TELEGRAM_ALLOWED_USER_IDS' into a set of ints. Ignores blanks/junk."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def is_allowed(user_id: int, allowed: set[int]) -> bool:
    """True only if the allowlist is configured AND the user is on it."""
    return bool(allowed) and user_id in allowed


def build_session_id(chat_id: int, thread_id: int | None) -> str:
    """Map a Telegram chat (+ forum topic) to a stable autopilot session id."""
    return f"tg:{chat_id}:{thread_id or 0}"


_HANDOFF_PLAN_RE = re.compile(r"`([^`]+\.md)`")
_HANDOFF_REGISTRY_RAW = "https://raw.githubusercontent.com/TrueSightDAO/agentic_ai_context/main/SOPHIA_HANDOFFS.md"


def _handoff_plan_for_thread(thread_id: int | None) -> str | None:
    """Resolve the active handoff plan file for a Telegram topic via the registry.

    Each forum topic that is a handoff has a row in
    agentic_ai_context/SOPHIA_HANDOFFS.md mapping thread_id -> plan file. A bare
    governor message like "go for it" carries no context on its own, so we look
    up the current thread_id here and let the brain load the plan. Fail-safe:
    any error returns None and the message is dispatched unchanged.

    Reads the local synced clone first (fast), then falls back to GitHub `main`
    on a miss — a handoff registered since the last ~5-min context sync exists on
    GitHub before it reaches the clone, and that freshness window is exactly when
    a governor types "go for it" (caused the 2026-06-09 "no context" miss).
    """
    if not thread_id:
        return None
    # 1) local synced clone — fast, no network
    try:
        reg = settings.context_repos_dir / "agentic_ai_context" / "SOPHIA_HANDOFFS.md"
        plan = _parse_handoff_plan(reg.read_text(encoding="utf-8"), thread_id)
        if plan:
            return plan
    except Exception:  # noqa: BLE001 — context enrichment must never break dispatch
        pass
    # 2) GitHub main fallback — covers a just-registered handoff not yet synced
    try:
        resp = httpx.get(_HANDOFF_REGISTRY_RAW, timeout=8.0)
        if resp.status_code == 200:
            return _parse_handoff_plan(resp.text, thread_id)
    except Exception:  # noqa: BLE001
        pass
    return None


def _parse_handoff_plan(registry_text: str, thread_id: int) -> str | None:
    """Pure parse: find the active handoff plan file for thread_id in the
    SOPHIA_HANDOFFS.md registry table. Matches the bare thread_id column or the
    session_id cell (tg:<chat>:<id>); requires the row's status to be active."""
    for line in registry_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        matched = any(
            c.strip("`") == str(thread_id) or c.strip("`").endswith(f":{thread_id}")
            for c in cells
        )
        if not matched:
            continue
        if not any("active" in c.lower() for c in cells):
            continue
        m = _HANDOFF_PLAN_RE.search(line)
        if m:
            return m.group(1)
    return None


_GO_SIGNAL_RE = re.compile(
    r"(\bgo for it\b|\bgo ahead\b|\bproceed\b|\bship it\b|\bexecute\b|\bresume\b|"
    r"\bcontinue\b|\bpick (it|this) up\b|\bkick (it )?off\b|\blet'?s go\b|"
    r"\bplan\b|\bhandoff\b|\bmission\b|RESUME HERE|^\s*go\s*$)",
    re.IGNORECASE,
)


def _looks_like_go_signal(text: str | None) -> bool:
    """A go-signal or plan/handoff reference — the ONLY case where an *unregistered*
    thread should still get the generic handoff fallback. Ordinary chat (e.g. a
    Stream-of-consciousness thread) → no handoff prefix, so the model isn't pushed
    to 'go find a plan' on a normal message (2026-06-12)."""
    if not text:
        return False
    return bool(_GO_SIGNAL_RE.search(text))


def _handoff_prefix(thread_id: int | None, text: str = "") -> str:
    """Context block to prepend so a bare go-signal in a handoff topic resolves.
    Registered handoffs always get their plan context; an *unregistered* thread only
    gets the generic fallback when the message looks like a go-signal/plan reference."""
    if not thread_id:
        return ""
    plan = _handoff_plan_for_thread(thread_id)
    if plan:
        return (
            f"[Handoff context — auto-injected from SOPHIA_HANDOFFS.md: this Telegram "
            f"topic (thread {thread_id}) is the active handoff for `{plan}`. Before "
            f'responding, read it with read_context_file("{plan}") and resume from its '
            f'RESUME HERE marker. Treat a short go-signal in this topic ("go for it", '
            f'"go", "proceed", "ship it") as the governor\'s full authorization to '
            f"execute that plan through its gates, reporting progress in this topic.]\n\n"
        )
    # Generic fallback — registry lookup missed (unregistered topic, parse/format
    # change, or both clone AND GitHub unreachable). Only inject it when the message
    # looks like a go-signal / plan reference: a normal chat message on a non-handoff
    # thread should NOT be told "this may be a handoff, go find a plan" (that noise
    # confused the model on the Stream-of-consciousness thread, 2026-06-12).
    if not _looks_like_go_signal(text):
        return ""
    return (
        f"[Handoff context — this Telegram topic (thread {thread_id}) may be an "
        f'execution handoff. If the governor gives a go-signal ("go for it", "go", '
        f'"proceed") or references a plan/mission, find this thread in '
        f"agentic_ai_context/HANDOFF_MANIFEST.md + SOPHIA_HANDOFFS.md via "
        f"read_context_file, open the referenced `*_PLAN.md`, and resume from its "
        f"RESUME HERE. Do NOT reply that you lack context without checking the "
        f"registry first.]\n\n"
    )


def chunk_text(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split a long reply into Telegram-sized chunks, preferring line breaks."""
    text = text if (text and text.strip()) else "(no response)"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_US_RE = re.compile(r"__([^_\n]+)__")
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_URL_RE = re.compile(r"https?://[^\s)}\]>]+")


def markdown_to_telegram_html(md: str) -> str:
    """Convert the subset of Markdown LLMs emit into Telegram-supported HTML.

    Telegram has no headings, uses <b>/<i>/<code>/<pre>/<a>, and only needs
    &, <, > escaped in text. Strategy: stash code (escaped) behind placeholders,
    escape the rest, then map headings/bold/bullets/links to tags, then restore.
    """
    stash: dict[str, str] = {}

    def _store(inner: str, tag: str) -> str:
        key = f"@@TGCODE{len(stash)}@@"
        stash[key] = f"<{tag}>{html.escape(inner, quote=False)}</{tag}>"
        return key

    text = _FENCE_RE.sub(lambda m: _store(m.group(1).rstrip("\n"), "pre"), md)
    text = _INLINE_CODE_RE.sub(lambda m: _store(m.group(1), "code"), text)
    text = html.escape(text, quote=False)  # escape remaining &<>
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    lines: list[str] = []
    for line in text.split("\n"):
        h = _HEADER_RE.match(line)
        if h:
            # header is already bold; strip inner **/__ so the bold pass below
            # doesn't produce invalid nested <b><b>…</b></b> (Telegram 400s on it)
            inner = h.group(1).replace("**", "").replace("__", "")
            lines.append(f"<b>{inner}</b>")
            continue
        b = _BULLET_RE.match(line)
        if b:
            lines.append(f"{b.group(1)}• {b.group(2)}")
            continue
        lines.append(line)
    text = "\n".join(lines)

    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_US_RE.sub(r"<b>\1</b>", text)

    for key, val in stash.items():
        text = text.replace(key, val)
    return text


# ── URL extraction ─────────────────────────────────────────────────────────


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text. Returns unique, ordered list."""
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".),;!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ── Telegram + chat I/O ────────────────────────────────────────────────────


def _api(method: str) -> str:
    return f"{_TELEGRAM_API}/bot{settings.telegram_bot_api_key}/{method}"


def get_updates(offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": _POLL_TIMEOUT}
    if offset is not None:
        params["offset"] = offset
    resp = httpx.get(_api("getUpdates"), params=params, timeout=_POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json().get("result", [])


def extract_attachment_file_id(msg: dict[str, Any]) -> str | None:
    """Return the Telegram file_id of a photo (largest size) or document, if any."""
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        return (photos[-1] or {}).get("file_id")  # last entry = highest resolution
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        return doc["file_id"]
    return None


def extract_voice_file_id(msg: dict[str, Any]) -> str | None:
    """Return the file_id of a voice note / audio / video-note, if any."""
    for key in ("voice", "audio", "video_note"):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            return obj["file_id"]
    return None


def download_telegram_file(file_id: str) -> str | None:
    """Download a Telegram file to a local path the autopilot tools can read.
    Returns the absolute path, or None on failure."""
    try:
        meta = httpx.get(_api("getFile"), params={"file_id": file_id}, timeout=20.0)
        meta.raise_for_status()
        file_path = (meta.json().get("result") or {}).get("file_path")
        if not file_path:
            return None
        ext = os.path.splitext(file_path)[1] or ".bin"
        os.makedirs(_ATTACH_DIR, exist_ok=True)
        dest = os.path.join(_ATTACH_DIR, f"{uuid.uuid4().hex}{ext}")
        url = f"{_TELEGRAM_API}/file/bot{settings.telegram_bot_api_key}/{file_path}"
        with httpx.stream("GET", url, timeout=60.0) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        logger.info("downloaded telegram attachment → %s", dest)
        return dest
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram file download failed: %s", e)
        return None


def send_message(chat_id: int, text: str, thread_id: int | None = None) -> int | None:
    """Send a message; return the Telegram message_id of the first chunk, or None.

    Retries up to 3 times on 429 (rate limited) with exponential backoff,
    respecting Telegram's retry_after hint.
    """
    msg_id: int | None = None
    for i, chunk in enumerate(chunk_text(text)):
        # Render Markdown → Telegram HTML so headings/bold/bullets/code show up.
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": markdown_to_telegram_html(chunk),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
        for attempt in range(3):
            try:
                resp = httpx.post(_api("sendMessage"), json=payload, timeout=20.0)
                if resp.status_code == 200:
                    result = resp.json().get("result", {})
                    if i == 0:
                        msg_id = result.get("message_id")
                    break
                elif resp.status_code == 429:
                    retry_after = (
                        resp.json().get("parameters", {}).get("retry_after", 5)
                    )
                    logger.warning(
                        "sendMessage 429 (attempt %d/3): retry after %ds",
                        attempt + 1,
                        retry_after,
                    )
                    time.sleep(min(retry_after + 1, 30))
                else:
                    logger.warning(
                        "sendMessage %s: %s", resp.status_code, resp.text[:200]
                    )
                    # Fallback: send the raw chunk as plain text. Covers both
                    # "message thread not found" and any HTML parse error.
                    # NOTE: do NOT include message_thread_id in the fallback — if the
                    # original 400 was "message thread not found", the retry with the
                    # same thread_id would 400 again. Drop it so the message lands.
                    fallback: dict[str, Any] = {
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    }
                    resp2 = httpx.post(_api("sendMessage"), json=fallback, timeout=20.0)
                    if i == 0 and resp2.status_code == 200:
                        msg_id = resp2.json().get("result", {}).get("message_id")
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning("sendMessage failed (attempt %d/3): %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(2**attempt)
    return msg_id


def send_voice(chat_id: int, file_path: str, thread_id: int | None = None) -> bool:
    """Send a voice message from a local audio file. Returns True on success.

    Retries up to 3 times on 429 (rate limited) with exponential backoff,
    respecting Telegram's retry_after hint.
    """
    if not os.path.isfile(file_path):
        logger.warning("send_voice: file not found %s", file_path)
        return False
    for attempt in range(3):
        try:
            with open(file_path, "rb") as fh:
                files = {"voice": (os.path.basename(file_path), fh, "audio/mpeg")}
                payload: dict[str, Any] = {"chat_id": chat_id}
                if thread_id:
                    payload["message_thread_id"] = thread_id
                resp = httpx.post(
                    _api("sendVoice"), data=payload, files=files, timeout=30.0
                )
                if resp.status_code == 200:
                    logger.info(
                        "Sent voice message (%d bytes) to chat %s",
                        os.path.getsize(file_path),
                        chat_id,
                    )
                    return True
                elif resp.status_code == 429:
                    retry_after = (
                        resp.json().get("parameters", {}).get("retry_after", 5)
                    )
                    logger.warning(
                        "sendVoice 429 (attempt %d/3): retry after %ds",
                        attempt + 1,
                        retry_after,
                    )
                    time.sleep(min(retry_after + 1, 30))
                else:
                    logger.warning(
                        "sendVoice %s: %s", resp.status_code, resp.text[:200]
                    )
                    return False
        except Exception as e:
            logger.warning("sendVoice failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2**attempt)
    return False


def edit_message_text(
    chat_id: int, message_id: int, text: str, thread_id: int | None = None
) -> bool:
    """Edit a previously sent message. Returns True on success."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": markdown_to_telegram_html(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        resp = httpx.post(_api("editMessageText"), json=payload, timeout=10.0)
        return resp.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("editMessageText failed: %s", e)
        return False


def delete_message(chat_id: int, message_id: int) -> bool:
    """Delete a message. Returns True on success."""
    try:
        resp = httpx.post(
            _api("deleteMessage"),
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def send_typing(chat_id: int, thread_id: int | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        httpx.post(_api("sendChatAction"), json=payload, timeout=10.0)
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def send_voice_action(chat_id: int, thread_id: int | None = None) -> None:
    """Send 'uploading voice' chat action so Telegram shows a mic indicator."""
    payload: dict[str, Any] = {"chat_id": chat_id, "action": "record_voice"}
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        httpx.post(_api("sendChatAction"), json=payload, timeout=10.0)
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def resolve_governor_public_key() -> str | None:
    """Find the public key of the configured governor name in the DAO registry."""
    target = settings.telegram_governor_name.strip()
    for g in load_governors().get("governors", []):
        if g.get("name", "").strip() == target and g.get("public_key"):
            return g["public_key"]
    return None


def resolve_governor_chat_id() -> int | None:
    """Resolve the Telegram chat ID for the configured governor.

    Uses the DEPLOY_NOTIFY_CHAT_ID setting if set, otherwise falls back
    to the first allowed user ID from TELEGRAM_ALLOWED_USER_IDS.
    Returns None if neither is available.
    """
    deploy_chat = os.getenv("DEPLOY_NOTIFY_CHAT_ID", "").strip()
    if deploy_chat.lstrip("-").isdigit():
        return int(deploy_chat)
    allowed = parse_allowed_ids(settings.telegram_allowed_user_ids)
    if allowed:
        return next(iter(allowed))
    return None


def send_deploy_notification(commit: str, elapsed_seconds: float) -> bool:
    """Send a 'deploy complete' notification to the governor's Telegram chat.

    Called by the NEW process after startup (from main.py lifespan) when
    a deploy marker file is found. Returns True on success.

    The message is sent directly via the Telegram Bot API — no JWT or
    /chat-blocking needed since this is a standalone notification.
    """
    if not settings.telegram_bot_api_key:
        logger.warning(
            "send_deploy_notification: TELEGRAM_BOT_API_KEY not set — skipping"
        )
        return False

    chat_id = resolve_governor_chat_id()
    if chat_id is None:
        logger.warning("send_deploy_notification: no chat ID available — skipping")
        return False

    commit_short = commit[:7] if commit and commit != "unknown" else "unknown"
    text = (
        f"✅ <b>Autopilot deploy complete</b>\n"
        f"• Commit: <code>{commit_short}</code>\n"
        f"• Back online in {elapsed_seconds}s"
    )

    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = httpx.post(
            f"{_TELEGRAM_API}/bot{settings.telegram_bot_api_key}/sendMessage",
            json=payload,
            timeout=20.0,
        )
        if resp.status_code == 200:
            logger.info("Deploy notification sent to chat %s", chat_id)
            return True
        else:
            logger.warning(
                "send_deploy_notification HTTP %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            return False
    except Exception as e:
        logger.warning("send_deploy_notification failed: %s", e)
        return False


_DEPLOY_MARKER = "/tmp/.autopilot_deployed"


def _deploy_in_progress() -> bool:
    """The deploy marker exists between deploy.py writing it (just before the
    restart) and the freshly-booted brain removing it — i.e. a redeploy is underway."""
    try:
        return os.path.exists(_DEPLOY_MARKER)
    except Exception:
        return False


def _wait_for_brain(max_attempts: int = 5, backoff: float = 2.5) -> bool:
    """Wait for the brain to be reachable (it may be mid deploy-restart), so a brief
    restart is invisible instead of surfacing a Connection-refused error. Returns fast
    when the brain is already up."""
    url = f"{settings.autopilot_chat_url.rstrip('/')}/health"
    for attempt in range(max_attempts):
        try:
            if httpx.get(url, timeout=5.0).status_code == 200:
                return True
        except Exception:
            pass
        if attempt < max_attempts - 1:
            time.sleep(backoff)
    return False


def _brain_unavailable_message() -> str:
    """A clear indicator instead of a raw Errno — names a redeploy when one is underway."""
    if _deploy_in_progress():
        return "🚀 Sophia is redeploying — back in a few seconds. Please resend your message shortly."
    return "⏳ Sophia is briefly restarting — please resend in a few seconds."


def call_chat(message: str, session_id: str, public_key: str) -> str:
    """POST to /chat-blocking as the governor; return the assistant text."""
    if not _wait_for_brain():
        return _brain_unavailable_message()
    token = create_jwt(public_key)
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}
    resp = httpx.post(
        f"{settings.autopilot_chat_url.rstrip('/')}/chat-blocking",
        json={"message": message},
        headers=headers,
        timeout=_CHAT_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("chat-blocking HTTP %s: %s", resp.status_code, resp.text[:300])
        return f"⚠️ Autopilot returned HTTP {resp.status_code}."
    data = resp.json()
    text = (data.get("response") or "").strip()
    if not text:
        text = "⚠️ Autopilot returned an empty response. Try rephrasing, or break the request into smaller steps."
    if data.get("proposal"):
        text += (
            "\n\n⚠️ This action needs approval — open the DApp chat to approve/reject."
        )
    return text


def call_chat_with_progress(
    chat_id: int, thread_id: int | None, message: str, session_id: str, public_key: str
) -> tuple[str, bool]:
    """POST to /chat (SSE) and send styled interim progress updates to Telegram.

    Flow:
      1. Send a styled status message with message_id captured.
      2. Stream SSE events, editing the status message with progress.
      3. On 'done': send the final response as a new message, delete the status.

    Returns:
        (response_text, text_already_displayed). The bool is True when this
        function has already rendered the text reply into the chat (the normal
        path edits/sends the final answer). It is False only when it fell back
        to the blocking call_chat, which returns text WITHOUT displaying it —
        the caller (voice path) must then send the text itself. This prevents
        the duplicate-text bug where the answer was sent twice.
    """
    token = create_jwt(public_key)
    headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}

    status_id = send_message(chat_id, "🔄 Thinking…", thread_id)
    if status_id is None:
        logger.warning("Could not send status message — falling back to blocking chat")
        # Blocking fallback returns text but does NOT display it → caller sends it.
        return call_chat(message, session_id, public_key), False

    # Ride out a brain restart (e.g. a redeploy) before streaming, so a brief
    # redeploy shows a clear indicator instead of a Connection-refused error.
    if not _wait_for_brain():
        msg = _brain_unavailable_message()
        edit_message_text(chat_id, status_id, msg, thread_id)
        return msg, True

    tool_emoji: dict[str, str] = {
        "web_search": "🔍",
        "web_extract": "📄",
        "read_context_file": "📚",
        "read_repo_file": "📖",
        "read_local_file": "📂",
        "list_directory": "📁",
        "list_org_repos": "🗂",
        "list_prs": "📋",
        "scan_qr_from_file": "📸",
        "scan_qr_batch": "📸",
        "lookup_qr_code": "🔎",
        "lookup_qr_batch": "🔎",
        "submit_contribution": "📝",
        "open_fix_pr": "🔧",
        "create_dao_submission": "📝",
        "upload_file_to_github": "📤",
        "merge_pr": "✅",
        "register_identity": "🆔",
        "deploy_autopilot": "🚀",
        "read_oracle_logs": "🔮",
    }

    def _label_tool(name: str) -> str:
        label = name.replace("_", " ")
        emoji = tool_emoji.get(name, "⚙️")
        return f"{emoji} {label} …"

    round_num = 0
    tool_active: str | None = None
    thinking_text: str = ""
    last_edit = time.time()

    try:
        with httpx.stream(
            "POST",
            f"{settings.autopilot_chat_url.rstrip('/')}/chat",
            json={"message": message},
            headers=headers,
            timeout=_CHAT_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                edit_message_text(
                    chat_id,
                    status_id,
                    f"⚠️ Autopilot returned HTTP {resp.status_code}.",
                    thread_id,
                )
                return f"⚠️ Autopilot returned HTTP {resp.status_code}.", True

            final_response = ""
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "heartbeat":
                    event.get("phase", "llm")
                    r = event.get("round", 0)
                    if r != round_num:
                        round_num = r
                        tool_active = None
                        thinking_text = ""
                    if tool_active is None:
                        if thinking_text:
                            snippet = thinking_text.replace("\n", " ")[:80]
                            _msg = f"💭 {snippet}…"
                        elif round_num > 0:
                            _msg = f"🔄 Thinking… (round {round_num})"
                        else:
                            _msg = "🔄 Thinking…"
                    else:
                        _msg = "🔄 Thinking…"  # fallback, tool label takes over
                    if time.time() - last_edit > 3:
                        edit_message_text(chat_id, status_id, _msg, thread_id)
                        last_edit = time.time()

                elif etype == "token":
                    content = event.get("content", "")
                    if content and not tool_active:
                        # The LLM is thinking out loud before calling tools — capture it
                        thinking_text = (thinking_text + content).strip()

                elif etype == "tool":
                    tool_name = event.get("tool", "")
                    status = event.get("status", "")
                    if status == "calling":
                        tool_active = tool_name
                        thinking_text = ""
                        _msg = _label_tool(tool_name)
                        edit_message_text(chat_id, status_id, _msg, thread_id)
                        last_edit = time.time()
                    elif status == "done":
                        tool_active = None

                elif etype == "wanted_more_rounds":
                    edit_message_text(
                        chat_id,
                        status_id,
                        "⚠️ Hit round limit — forcing final response…",
                        thread_id,
                    )
                    last_edit = time.time()

                elif etype == "error":
                    # The /chat stream raised before producing a `done` (e.g. the LLM
                    # 400s on a malformed-history turn). Surface it instead of the bare
                    # "empty response" banner — the corrupted transcript self-heals on
                    # the next turn via _sanitise_tool_messages, so a resend works.
                    err = (
                        event.get("content") or "the LLM rejected the request"
                    ).strip()
                    if len(err) > 300:
                        err = err[:300] + "…"
                    final_response = (
                        "⚠️ Autopilot hit an error (the thread self-heals — please "
                        f"resend): {err}"
                    )
                    break

                elif etype == "done":
                    final_response = (event.get("response") or "").strip()
                    if event.get("proposal"):
                        final_response += "\n\n⚠️ This action needs approval — open the DApp chat to approve/reject."
                    break

            # Replace status message with final response
            if final_response:
                if len(final_response) <= _MESSAGE_LIMIT and edit_message_text(
                    chat_id, status_id, final_response, thread_id
                ):
                    return final_response, True
                delete_message(chat_id, status_id)
                send_message(chat_id, final_response, thread_id)
                return final_response, True
            else:
                edit_message_text(
                    chat_id,
                    status_id,
                    "⚠️ Autopilot produced an empty response.",
                    thread_id,
                )
                return "⚠️ Autopilot produced an empty response.", True

    except httpx.ReadTimeout:
        edit_message_text(
            chat_id,
            status_id,
            "⚠️ Autopilot timed out. Try a simpler request or try again.",
            thread_id,
        )
        return "⚠️ Autopilot timed out — the LLM or a tool took too long.", True
    except Exception as e:
        logger.exception("call_chat_with_progress failed")
        edit_message_text(chat_id, status_id, f"⚠️ Error: {e}", thread_id)
        return f"⚠️ Error: {e}", True


def call_chat_with_typing(
    chat_id: int, thread_id: int | None, message: str, session_id: str, public_key: str
) -> str:
    """Run call_chat in a worker thread, re-sending the 'typing…' action every
    few seconds so the indicator stays alive for the whole (often 30-60s+)
    multi-round generation instead of vanishing after ~5s."""
    send_typing(chat_id, thread_id)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(call_chat, message, session_id, public_key)
        while True:
            try:
                return future.result(timeout=_TYPING_INTERVAL)
            except concurrent.futures.TimeoutError:
                send_typing(chat_id, thread_id)  # keep the indicator alive


# ── Voice reply helpers ────────────────────────────────────────────────────


def _handle_voice_reply(
    chat_id: int,
    thread_id: int | None,
    assistant_response: str,
    transcribed_text: str | None = None,
    text_already_sent: bool = True,
) -> None:
    """Synthesize and send a voice reply alongside the text reply.

    The text reply is normally already rendered by ``call_chat_with_progress``
    (it edits the "🔄 Thinking…" status into the final answer). In that case we
    must NOT re-send the full text here — doing so caused the duplicate-text
    bug (text → voice → the same text again). We only add a links-only
    follow-up so the URLs are clickable, since the voice note does not read
    URLs aloud.

    When ``text_already_sent`` is False (the rare progress fallback that returns
    text without displaying it), this function sends the full text itself.

    For voice messages, language is detected from the user's transcribed text.
    For text messages, language is detected from the assistant's response.

    Args:
        chat_id: Telegram chat ID.
        thread_id: Optional forum thread ID.
        assistant_response: The autopilot's text response to synthesize.
        transcribed_text: The user's transcribed voice message (for language detection).
            If None, language is detected from assistant_response instead.
        text_already_sent: True if the text reply has already been displayed in
            the chat (the normal progress path). When True, only a links-only
            follow-up is sent; when False, the full text is sent.
    """
    # Detect language: prefer transcribed_text (user's voice), fall back to response text
    source_for_lang = transcribed_text if transcribed_text else assistant_response
    lang = detect_language(source_for_lang)
    voice_name = {"en": "Aria", "zh": "Xiaoxiao", "pt": "Francisca"}.get(lang, "Aria")

    # Show recording action so Telegram shows a mic icon
    send_voice_action(chat_id, thread_id)

    # Synthesize the response
    mp3_path = synthesize_voice(assistant_response, language=lang)
    if mp3_path:
        send_voice(chat_id, mp3_path, thread_id)
        logger.info(
            "Sent voice reply: lang=%s voice=%s text_len=%d",
            lang,
            voice_name,
            len(assistant_response),
        )
    else:
        logger.warning("Voice synthesis failed, skipping voice")

    # Text handling: avoid duplicating the answer that the progress path showed.
    urls = extract_urls(assistant_response)
    if text_already_sent:
        # Full text already rendered by call_chat_with_progress. Only add a
        # links-only follow-up so the URLs are clickable (voice skips URLs).
        if urls:
            url_text = "**🔗 Links from my response:**\n"
            for url in urls:
                url_text += f"\n• {url}"
            send_message(chat_id, url_text, thread_id)
    else:
        # Text was NOT displayed yet (progress fallback) — send it now.
        if urls:
            url_text = "**🔗 Links from my response:**\n"
            for url in urls:
                url_text += f"\n• {url}"
            send_message(chat_id, assistant_response + "\n\n" + url_text, thread_id)
        else:
            send_message(chat_id, assistant_response, thread_id)


# ── Update handling + loop ─────────────────────────────────────────────────


def _auto_process_attachment(
    local_path: str, chat_id: int, thread_id: int | None, session_id: str
) -> str | None:
    """Auto-detect file type, extract content, persist to transcript, return summary.

    Returns a summary string for the LLM, or None on failure.
    Sends progress updates to Telegram as it works.
    """
    import subprocess
    import sys
    from pathlib import Path

    SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
    ext = Path(local_path).suffix.lower()
    pdf_exts = {".pdf"}
    image_exts = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}

    status_id = send_message(chat_id, "📄 Processing attachment…", thread_id)

    def _update_status(msg: str) -> None:
        if status_id:
            edit_message_text(chat_id, status_id, msg, thread_id)

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
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- PDF path ---
    if ext in pdf_exts:
        _update_status("📄 Extracting PDF text…")
        pdf_result = _run_script("extract_pdf_text.py", local_path)

        if pdf_result.get("status") != "success":
            _update_status(
                f"⚠️ PDF extraction failed: {pdf_result.get('message', 'unknown error')}"
            )
            return None

        page_count = pdf_result.get("page_count", 0)
        total_chars = pdf_result.get("total_chars", 0)
        is_scanned = pdf_result.get("likely_scanned_pdf", False)

        # Build extracted text content
        pages_text = []
        for p in pdf_result.get("pages", []):
            t = p.get("text", "").strip()
            if t:
                pages_text.append(f"--- Page {p['page']} ---\n{t}")
        extracted_text = "\n\n".join(pages_text)

        # If scanned PDF, run OCR too
        ocr_text = ""
        if is_scanned:
            _update_status("📄 PDF appears scanned — running OCR…")
            ocr_result = _run_script("ocr_image.py", local_path, "eng")
            if ocr_result.get("status") == "success":
                ocr_text = ocr_result.get("text", "")
                extracted_text += f"\n\n--- OCR of scanned PDF ---\n{ocr_text}"

        # Do NOT persist the transcript from here. A separate process writing the
        # session file while a turn holds the history in memory clobbers it — the
        # cross-process race that bricked threads 3 and 780. Return the full
        # extracted content inline instead: it rides in the dispatched message and
        # the turn (the single writer) appends it under the per-session lock.
        filename = Path(local_path).name
        summary = (
            f"[Attachment auto-processed: **{filename}**]\n"
            f"- Type: PDF ({page_count} page{'s' if page_count != 1 else ''}, {total_chars} chars)\n"
        )
        if is_scanned:
            summary += (
                f"- Scanned PDF: OCR also applied ({len(ocr_text)} chars extracted)\n"
            )
        summary += f"\nExtracted content:\n```\n{extracted_text[:45000]}\n```\n"
        if len(extracted_text) > 45000:
            summary += "\n*(content truncated to 45000 chars)*\n"

        _update_status(
            f"✅ Extracted {page_count} page{'s' if page_count != 1 else ''} from PDF"
        )
        return summary

    # --- Image path ---
    if ext in image_exts:
        _update_status("📸 Running OCR on image…")
        ocr_result = _run_script("ocr_image.py", local_path, "eng")

        if ocr_result.get("status") != "success":
            _update_status(
                f"⚠️ OCR failed: {ocr_result.get('message', 'unknown error')}"
            )
            return None

        extracted_text = ocr_result.get("text", "")
        confidence = ocr_result.get("avg_confidence", 0)
        quality = ocr_result.get("quality", "unknown")

        # Do NOT persist the transcript from here (see the PDF branch). Return the
        # content inline; the turn is the single writer that appends it under the
        # per-session lock.
        filename = Path(local_path).name
        summary = (
            f"[Attachment auto-processed: **{filename}**]\n"
            f"- Type: Image (OCR confidence: {confidence}%, quality: {quality})\n"
        )
        if extracted_text:
            summary += f"\nExtracted text:\n```\n{extracted_text[:45000]}\n```\n"
        else:
            summary += "\n*(No text detected in image)*\n"

        _update_status(f"✅ OCR complete (confidence: {confidence}%)")
        return summary

    # --- Unknown file type ---
    _update_status(f"⚠️ Unknown file type: {ext}")
    return None


# ── Identity verification on-ramp (Phase 1) ──────────────────────────────
# Maps a Telegram user_id mid-verification to the email they're verifying.
# A pending entry routes the user's next DM (the emailed code) to consume.
_pending_verifications: dict[int, dict[str, Any]] = {}
_VERIFY_PENDING_TTL = 15 * 60  # match identity_binding's challenge expiry


def _maybe_handle_verification(
    chat_id: int, user_id: int, username: str | None, text: str
) -> bool:
    """DM-only identity verification on-ramp (Phase 1).

    Open to UNBOUND users (runs before the allowlist gate) but strictly
    limited to: starting `/verify <email>`, pasting a code while one is
    pending, or `/cancel`. Anything else returns False so the caller falls
    through to the normal security gate. The email + code exchange happens
    only in DM so they are never group-visible.

    Returns True iff the message was consumed by the verification flow.
    """
    from .identity_binding import consume_challenge, mint_challenge

    # Expire a stale pending verification.
    pending = _pending_verifications.get(user_id)
    if pending and (time.time() - pending["ts"]) > _VERIFY_PENDING_TTL:
        _pending_verifications.pop(user_id, None)
        pending = None

    # Start a new verification.
    if text.startswith("/verify"):
        email = text[len("/verify") :].strip()
        if not email or "@" not in email:
            send_message(
                chat_id,
                "Usage: `/verify you@example.com`\n\n"
                "I'll email an 8-character code to that address (it must be on "
                "file in the DAO ledger). Paste the code back here to link this "
                "Telegram account to your contributor identity.",
            )
            return True
        result = mint_challenge(email, telegram_id=user_id)
        if result.get("success"):
            _pending_verifications[user_id] = {"email": email, "ts": time.time()}
            send_message(
                chat_id,
                f"📧 If *{email}* is registered, I've emailed it an 8-character "
                "code (valid 15 minutes). Paste it here to finish linking. "
                "Send /cancel to stop.",
            )
        else:
            send_message(
                chat_id,
                f"⚠️ {result.get('error', 'Could not start verification.')}",
            )
        return True

    if not pending:
        return False

    # Pending verification: handle cancel, then treat the message as the code.
    if text.lower() in ("/cancel", "cancel"):
        _pending_verifications.pop(user_id, None)
        send_message(chat_id, "Verification cancelled.")
        return True
    if text.startswith("/"):
        # Let other slash-commands through rather than burning an attempt.
        return False

    code = text.strip().upper().replace(" ", "")
    result = consume_challenge(pending["email"], code, user_id, username)
    if result.get("success"):
        _pending_verifications.pop(user_id, None)
        # New binding — bust the policy binding cache so it takes effect now.
        try:
            from .policy import refresh_governor_cache

            refresh_governor_cache()
        except Exception:  # noqa: BLE001
            pass
        send_message(
            chat_id,
            f"✅ Verified! This Telegram account is now linked to "
            f"*{pending['email']}* in the DAO ledger.",
        )
    else:
        send_message(chat_id, f"❌ {result.get('error', 'Verification failed.')}")
    return True


def handle_message(
    msg: dict[str, Any], allowed: set[int], public_key: str | None
) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    # Only treat a thread id as routable when it's a genuine forum topic.
    # Reply-threads and the General topic carry ids that 400 on sendMessage
    # ("message thread not found"), so we ignore them for routing + session keying.
    thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    # Best-effort: capture the forum-topic name when Telegram includes it (topic
    # created/edited service message, or a reply rooted at the topic-creation
    # message) so the vault status page can show + link named threads.
    if thread_id:
        _ftc = (
            msg.get("forum_topic_created")
            or msg.get("forum_topic_edited")
            or (msg.get("reply_to_message") or {}).get("forum_topic_created")
        )
        if isinstance(_ftc, dict) and _ftc.get("name"):
            try:
                from .topic_names import record_topic_name

                record_topic_name(thread_id, _ftc["name"])
            except Exception:
                pass
    sender = msg.get("from") or {}
    user_id = sender.get("id")
    username = sender.get("username")
    display_name = (
        " ".join(
            p for p in (sender.get("first_name"), sender.get("last_name")) if p
        ).strip()
        or None
    )
    chat_type = chat.get("type")
    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()
    attachment_file_id = extract_attachment_file_id(msg)
    voice_file_id = extract_voice_file_id(msg)
    if (
        chat_id is None
        or user_id is None
        or (not text and not attachment_file_id and not voice_file_id)
    ):
        return

    # Identity verification on-ramp (Phase 1): DM-only, open to unbound users.
    # Runs BEFORE the gate so a new contributor can link their identity; only
    # consumes /verify, a pending code paste, or /cancel.
    if (
        chat_type == "private"
        and text
        and _maybe_handle_verification(chat_id, user_id, username, text)
    ):
        return

    # Security gate
    if not is_allowed(user_id, allowed):
        # A governor verified via /verify (Column X → Governors cache) is admitted
        # even if their telegram_id was never added to the env allowlist.
        admitted = False
        try:
            from .policy import Role, resolve_identity

            ident = resolve_identity(
                telegram_id=user_id,
                telegram_username=username,
                display_name=display_name,
            )
            admitted = ident.role == Role.GOVERNOR
        except Exception:  # noqa: BLE001 — never let policy errors open the gate
            admitted = False

        if not admitted:
            if not allowed:
                # Bootstrap: no allowlist configured yet — reveal the sender's own ID.
                logger.warning(
                    "Unconfigured allowlist; message from user_id=%s", user_id
                )
                send_message(
                    chat_id,
                    f"Your Telegram user ID is {user_id}.\nAdd it to TELEGRAM_ALLOWED_USER_IDS and restart to enable me.",
                    thread_id,
                )
            else:
                logger.warning(
                    "Rejected message from non-allowlisted user_id=%s", user_id
                )
                send_message(chat_id, "⛔ Not authorized.", thread_id)
            return

    # Voice note → transcribe locally (faster-whisper)
    is_voice = bool(voice_file_id and not text)
    # Send voice reply when the user provided text content (typed, voice, or caption)
    # but skip for bare attachment drops with no words at all.
    has_user_text = bool(text or voice_file_id or caption)
    transcribed_text = ""
    if is_voice:
        local_audio = download_telegram_file(voice_file_id)
        transcribed_text = transcribe_voice(local_audio) if local_audio else ""
        if not transcribed_text:
            send_message(
                chat_id,
                "🎤 I could not make out any speech in that voice note.",
                thread_id,
            )
            return
        text = transcribed_text

    # Lightweight commands (skip voice reply for commands)
    if text in ("/start", "/help"):
        send_message(
            chat_id,
            "**TrueSight Autopilot** — your private DAO assistant.\n\n"
            "**Topics & Roles**\n"
            "Each Telegram topic can have its own role. On a new topic, I'll ask you to pick one:\n"
            "`1` Content Marketing Researcher\n"
            "`2` Event Coordinator\n"
            "`3` SRE / DevOps Engineer\n"
            "`4` Retailer Outreach Coordinator\n"
            "`5` Logistics Analyst\n"
            "`6` Inventory Manager\n"
            "`7` General DAO Assistant\n\n"
            "**Commands**\n"
            "`/help` — this message\n"
            "`/verify <email>` — (DM) link this Telegram account to your DAO "
            "ledger identity via an emailed code\n"
            "`/research <topic>` — autonomous CrewAI research (needs role 1 or 4)\n"
            "`/reset` — clear context, keep role, start fresh\n"
            "Type a role number anytime to switch roles.\n\n"
            "**Chat**\n"
            "Just type your request. I can search the web, read repos, "
            "scan QR codes, open PRs, and more — scoped to my active role.",
            thread_id,
        )
        return

    if text in ("/reset",):
        session_id = build_session_id(chat_id, thread_id)
        _handle_reset(chat_id, thread_id, session_id, public_key)
        return

    if text.startswith("/research"):
        _handle_research_command(chat_id, thread_id, text, public_key)
        return

    if text.startswith("/ship"):
        _handle_ship_command(chat_id, thread_id, text)
        return

    if public_key is None:
        send_message(
            chat_id, "⚠️ No governor identity configured on the server.", thread_id
        )
        return

    session_id = build_session_id(chat_id, thread_id)

    # Attachment (photo / document): auto-process before LLM dispatch
    if attachment_file_id:
        local_path = download_telegram_file(attachment_file_id)
        if not local_path:
            send_message(
                chat_id, "⚠️ Couldn't download that attachment from Telegram.", thread_id
            )
            return

        # Auto-process: detect type, extract, persist, get summary
        attachment_summary = _auto_process_attachment(
            local_path, chat_id, thread_id, session_id
        )

        # Build the message for the LLM
        msg_text = caption or text or "Please inspect the attached file."
        msg_text = _handoff_prefix(thread_id, msg_text) + msg_text
        if attachment_summary:
            msg_text += f"\n\n{attachment_summary}"
        else:
            msg_text += (
                f"\n\n[Attachment saved at {local_path} — use scan_qr_from_file / "
                f"scan_qr_batch for QR images, extract_pdf_text for PDFs, "
                f"ocr_image for text extraction from images, or read_local_file for text. "
                f"After processing, use append_to_transcript to persist the extracted content.]"
            )

        try:
            # Prep (download + extraction above) ran in parallel; the turn itself
            # is serialized per topic so it can't race a concurrent turn's writes.
            lock = _thread_dispatch_lock(chat_id, thread_id)
            _ack_queued_if_busy(chat_id, thread_id, lock, session_id, public_key)
            with lock:
                response, text_shown = call_chat_with_progress(
                    chat_id, thread_id, msg_text, session_id, public_key
                )
            # If user included text content, also send voice reply
            if has_user_text and response:
                _handle_voice_reply(
                    chat_id,
                    thread_id,
                    response,
                    transcribed_text if is_voice else None,
                    text_already_sent=text_shown,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("call_chat failed (attachment)")
            send_message(chat_id, f"⚠️ Error processing the attachment: {e}", thread_id)
        return

    # Voice messages: give the assistant channel context so it does not assume the DApp
    # and knows its reply is spoken back. Not part of the transcript shown to the user.
    dispatch_text = text
    if is_voice:
        dispatch_text = (
            text
            + " [System note: the user sent this as a VOICE message via the Telegram bot. Your text reply is automatically synthesized into a voice note and sent back, so answer naturally for speech and keep it concise. The user is on Telegram, NOT the DApp web chat -- do not claim otherwise. URLs are delivered separately as text, so do not read URLs aloud.]"
        )
    dispatch_text = _handoff_prefix(thread_id, dispatch_text) + dispatch_text

    # Prepend Telegram context so the LLM can reference chat_id and thread_id
    if thread_id:
        dispatch_text = f"[Telegram context: chat_id={chat_id}, thread_id={thread_id}] {dispatch_text}"
    else:
        dispatch_text = f"[Telegram context: chat_id={chat_id}] {dispatch_text}"
    try:
        # Serialize the turn per topic so rapid-fire messages to the same thread
        # queue instead of racing; different topics still run in parallel.
        lock = _thread_dispatch_lock(chat_id, thread_id)
        # PR2: progress-query immediate answer — lock-bypassing
        # If the lock is held (a turn is running) AND the message is a short
        # status-y phrase, answer immediately from the live-progress record
        # without queuing or waiting for the lock.
        if lock.locked() and _is_progress_query(dispatch_text):
            try:
                token = create_jwt(public_key)
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Session-Id": session_id,
                }
                resp = httpx.get(
                    f"{settings.autopilot_chat_url.rstrip('/')}/chat/progress",
                    headers=headers,
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("running") and data.get("snapshot"):
                        send_message(
                            chat_id,
                            f"📊 **Current progress:**\n{data['snapshot']}",
                            thread_id,
                        )
                    else:
                        send_message(
                            chat_id, "📊 Nothing running right now.", thread_id
                        )
                    return
            except Exception:
                pass  # fall through to normal queue if progress fetch fails

        _ack_queued_if_busy(chat_id, thread_id, lock, session_id, public_key)
        with lock:
            response, text_shown = call_chat_with_progress(
                chat_id, thread_id, dispatch_text, session_id, public_key
            )
        # Send voice reply for ALL governor messages (text and voice), not just voice.
        # The assistant's response is synthesized as speech and sent as a voice note.
        # For voice messages, language is detected from the user's transcribed text.
        # For text messages, language is detected from the assistant's response.
        if response:
            _handle_voice_reply(
                chat_id,
                thread_id,
                response,
                transcribed_text if is_voice else None,
                text_already_sent=text_shown,
            )
    except Exception as e:  # noqa: BLE001 — never crash the loop on one message
        logger.exception("call_chat failed")
        send_message(chat_id, f"⚠️ Error talking to autopilot: {e}", thread_id)


def _handle_research_command(
    chat_id: int, thread_id: int | None, text: str, public_key: str
) -> None:
    """Handle /research command — spawn autonomous CrewAI research."""
    topic = text[len("/research") :].strip()
    if not topic:
        send_message(
            chat_id,
            "Usage: `/research <topic>`\n\n"
            "Example: `/research ceremonial cacao consumer demographics USA 2025`\n\n"
            "You must have a research-enabled role set first (e.g. Content Marketing Researcher).",
            thread_id,
        )
        return

    # Check current role via session
    session_id = build_session_id(chat_id, thread_id)
    try:
        resp = httpx.get(
            f"{settings.autopilot_chat_url.rstrip('/')}/session",
            headers={"X-Public-Key": public_key, "X-Session-Id": session_id},
            timeout=10.0,
        )
        if resp.status_code != 200:
            send_message(
                chat_id,
                "⚠️ Could not check current role. Set a role first by chatting in this topic.",
                thread_id,
            )
            return
        session_data = resp.json()
        history = session_data.get("messages", [])
    except Exception:
        send_message(chat_id, "⚠️ Could not reach autopilot server.", thread_id)
        return

    # Find role from history
    from .roles import find_role_in_history

    role = find_role_in_history(history)
    if role is None:
        send_message(
            chat_id,
            "⚠️ No role set in this topic. Send any message first to pick a role.",
            thread_id,
        )
        return

    if not role.crewai_enabled:
        send_message(
            chat_id,
            f"⚠️ The **{role.name}** role doesn't support autonomous research.\n"
            f"Switch to a research-enabled role like Content Marketing Researcher.",
            thread_id,
        )
        return

    # Determine target repo
    target_repo = "go_to_market"  # default for research
    if role.key == "retailer_outreach":
        target_repo = "market_research"

    # Start autonomous research with progress
    status_id = send_message(
        chat_id,
        f"🚀 Starting autonomous research on:\n*{topic[:100]}*…\n\nInitialising CrewAI…",
        thread_id,
    )
    if status_id is None:
        return

    from .research import run_research_background

    def on_progress(msg: str) -> None:
        snippet = msg.replace("\n", " ")[:200]
        edit_message_text(
            chat_id, status_id, f"🔬 Researching…\n\n_{snippet}_", thread_id
        )

    def on_done(result: str) -> None:
        preview = result[:3000]
        more = (
            "\n\n…(truncated — report committed to repo)" if len(result) > 3000 else ""
        )
        edit_message_text(
            chat_id,
            status_id,
            f"📄 **Research complete!**\n\nTopic: {topic[:100]}\nRepo: `{target_repo}`\n\n---\n{preview}{more}",
            thread_id,
        )

    run_research_background(role.key, topic, target_repo, on_progress, on_done)


def _handle_reset(
    chat_id: int, thread_id: int | None, session_id: str, public_key: str
) -> None:
    """Reset session context: keep role tag, discard all other messages."""
    try:
        resp = httpx.get(
            f"{settings.autopilot_chat_url.rstrip('/')}/session",
            headers={"X-Public-Key": public_key, "X-Session-Id": session_id},
            timeout=10.0,
        )
        if resp.status_code != 200:
            send_message(chat_id, "⚠️ Could not access session.", thread_id)
            return
        history = resp.json().get("messages", [])
    except Exception:
        send_message(chat_id, "⚠️ Could not reach autopilot server.", thread_id)
        return

    from .roles import find_role_in_history

    role = find_role_in_history(history)
    # Build fresh history with just the role tag
    fresh: list[dict] = []
    if role:
        fresh = [{"role": "system", "content": f"[ROLE: {role.key}]"}]
        name = role.name
    else:
        name = "(no role)"

    # POST to a special internal endpoint to overwrite the session
    token = create_jwt(public_key)
    try:
        resp = httpx.post(
            f"{settings.autopilot_chat_url.rstrip('/')}/session/reset",
            json={"messages": fresh},
            headers={"Authorization": f"Bearer {token}", "X-Session-Id": session_id},
            timeout=10.0,
        )
        if resp.status_code == 200:
            send_message(
                chat_id,
                f"✅ Context reset. Role: **{name}**.\n\nWhat would you like to work on?",
                thread_id,
            )
        else:
            send_message(
                chat_id,
                f"⚠️ Could not reset session (HTTP {resp.status_code}).",
                thread_id,
            )
    except Exception as e:
        send_message(chat_id, f"⚠️ Reset failed: {e}", thread_id)


def send_message_with_keyboard(
    chat_id: int, text: str, keyboard: dict, thread_id: int | None = None
) -> None:
    """Send a message with an inline keyboard (HTML). Falls back to plain on error."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": markdown_to_telegram_html(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        resp = httpx.post(_api("sendMessage"), json=payload, timeout=20.0)
        if resp.status_code != 200:
            logger.warning(
                "sendMessage(keyboard) %s: %s", resp.status_code, resp.text[:200]
            )
            httpx.post(
                _api("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=20.0,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("sendMessage(keyboard) failed: %s", e)


def answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        httpx.post(
            _api("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10.0,
        )
    except Exception:  # noqa: BLE001
        pass


def _handle_ship_command(chat_id: int, thread_id: int | None, text: str) -> None:
    """B5/B6: /ship a beta PR. Lists open beta PRs, or confirms/ships a target."""
    from . import beta_deploy

    if not settings.beta_deploy_gate_enabled:
        send_message(
            chat_id,
            "🚦 Beta-deploy gate is **disabled**. Enable with "
            "`BETA_DEPLOY_GATE_ENABLED=true` to ship PRs to beta from here.",
            thread_id,
        )
        return
    target = beta_deploy.parse_ship_target(text)
    if target is None:
        prs = beta_deploy.list_open_beta_prs()
        if not prs:
            send_message(
                chat_id,
                "No open PRs on the beta repos. Ask me to make a change (I'll open a PR on a beta repo), then `/ship`.",
                thread_id,
            )
            return
        for pr in prs[:5]:
            kb = beta_deploy.build_ship_keyboard(pr["repo"], pr["number"])
            send_message_with_keyboard(
                chat_id,
                f"**{pr['repo']}#{pr['number']}** — {pr['title']}\n{pr['url']}",
                kb,
                thread_id,
            )
        return
    repo, pr = target
    if settings.beta_auto_merge:  # B6 — no tap
        result = beta_deploy.ship_pr(repo, pr)
        send_message(chat_id, result["message"], thread_id)
        return
    kb = beta_deploy.build_ship_keyboard(repo, pr)  # B5 — one-tap confirm
    send_message_with_keyboard(
        chat_id,
        f"Ship **{repo}#{pr}** to beta? I'll verify CI is green first.",
        kb,
        thread_id,
    )


def handle_callback_query(cb: dict[str, Any], allowed: set[int]) -> None:
    """Handle an inline-button tap (the beta-deploy 'Ship' / 'Cancel' buttons)."""
    from . import beta_deploy

    cb_id = cb.get("id", "")
    user_id = (cb.get("from") or {}).get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None

    answer_callback(cb_id)  # ack the tap so the spinner stops
    if not is_allowed(user_id, allowed):
        answer_callback(cb_id, "Not authorized")
        return

    action, repo, pr = beta_deploy.parse_callback_data(data)
    if action != "ship" or not repo or not pr:
        if chat_id and message_id:
            edit_message_text(chat_id, message_id, "✕ Cancelled.", thread_id)
        return

    if chat_id and message_id:
        edit_message_text(
            chat_id, message_id, f"⏳ Shipping {repo}#{pr} — checking CI…", thread_id
        )
    result = beta_deploy.ship_pr(repo, pr)
    if chat_id and message_id:
        edit_message_text(chat_id, message_id, result["message"], thread_id)
    elif chat_id:
        send_message(chat_id, result["message"], thread_id)


def _handle_callback_safe(cb: dict[str, Any], allowed: set[int]) -> None:
    try:
        handle_callback_query(cb, allowed)
    except Exception:  # noqa: BLE001
        logger.exception("handle_callback_query crashed")


def _handle_message_safe(
    msg: dict[str, Any], allowed: set[int], public_key: str | None
) -> None:
    """Wrap handle_message for background-thread dispatch so exceptions don't vanish."""
    try:
        handle_message(msg, allowed, public_key)
    except Exception:  # noqa: BLE001
        logger.exception("handle_message crashed")


def run() -> None:
    # Silence httpx INFO request logging — it prints the Telegram getUpdates/getFile/
    # sendMessage URLs, which embed the bot token. WARNING keeps errors, drops the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.telegram_bot_api_key:
        raise SystemExit(
            "TELEGRAM_BOT_API_KEY is not set — cannot start Telegram adapter."
        )

    allowed = parse_allowed_ids(settings.telegram_allowed_user_ids)
    public_key = resolve_governor_public_key()
    logger.info(
        "Telegram adapter starting: allowlist=%s governor=%s key_resolved=%s",
        sorted(allowed) or "(BOOTSTRAP — none set)",
        settings.telegram_governor_name,
        public_key is not None,
    )
    if not allowed:
        logger.warning(
            "No TELEGRAM_ALLOWED_USER_IDS set — running in bootstrap mode "
            "(replies with the sender's ID; does not call autopilot)."
        )
    if public_key is None:
        logger.warning(
            "Could not resolve a public key for governor '%s' — chat calls will be refused.",
            settings.telegram_governor_name,
        )

    offset: int | None = None
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=10, thread_name_prefix="tg-handle"
    ) as executor:
        while True:
            try:
                updates = get_updates(offset)
            except Exception as e:  # noqa: BLE001
                logger.warning("getUpdates failed: %s — backing off 5s", e)
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    executor.submit(_handle_message_safe, msg, allowed, public_key)
                cb = upd.get("callback_query")
                if cb:
                    executor.submit(_handle_callback_safe, cb, allowed)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
