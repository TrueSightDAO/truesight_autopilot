"""Resume-awaiting registry — message_id -> {thread_id, text}.

Enables the emoji-reaction go-signal (plans/SOPHIA_EMOJI_REACTION_GO_PLAN.md):

  * Sophia posts a "ready / resume here" proposal in a handoff topic, flagging
    each posted chunk's message_id as resume-awaiting (see the
    ``resume_awaiting=True`` hooks on ``send_message`` /
    ``create_telegram_topic`` / ``post_to_telegram_topic``).
  * Later (minutes to days), a governor reacts with a standard emoji to one of
    those messages. ``handle_message_reaction`` looks the message_id up here to
    recover the thread (the ``message_reaction`` update carries no
    ``message_thread_id``) and the resume text (decision 0.2/0.4).

Entries are persisted to a small JSON file next to ``_topic_names.json`` so the
registry survives a Sophia restart between the post and the reaction. Bounded:
entries are pruned once consumed or after a TTL (default 7 days).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from .config import settings

logger = logging.getLogger("autopilot.resume_registry")

_PATH = settings.session_log_dir / "_resume_awaiting.json"
_lock = threading.Lock()
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


def _load() -> dict:
    try:
        if _PATH.is_file():
            return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        tmp = _PATH.with_name("_resume_awaiting.json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception as e:  # never block a turn
        logger.debug("resume_registry save failed: %s", e)


def _prune(data: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        mid
        for mid, entry in data.items()
        if isinstance(entry, dict)
        and (entry.get("ts") or 0) < now - _DEFAULT_TTL_SECONDS
    ]
    for mid in expired:
        data.pop(mid, None)


_RESUME_HERE_RE = re.compile(r"RESUME HERE", re.IGNORECASE)


def looks_resume_awaiting(text: str | None) -> bool:
    """True when *text* itself declares a resume point — contains "RESUME HERE" — so a posted message carrying it is auto-flagged
    resume-awaiting even when the caller didn't pass resume_awaiting=True.

    Closes the 👍-on-RESUME-HERE gap (2026-09-02): the text go-signal regex
    (_GO_SIGNAL_RE) already matches "RESUME HERE" case-insensitively, so typing
    "go" resumed from such a message but a reaction on it did nothing — the
    registry only flagged messages at post-time when the caller passed
    resume_awaiting=True. Turn-reports carrying "📌 RESUME HERE" were ordinary
    posts, never flagged, so the registry lookup returned nothing and the
    reaction was ignored. Now any post whose text carries the resume point
    self-flags. (2026-09-03: the 📌 pin emoji ALONE does not flag — only
    the literal "RESUME HERE" text does.)
    """
    if not text:
        return False
    return bool(_RESUME_HERE_RE.search(text))


def mark_resume_awaiting(
    message_id: int | str, thread_id: int | str, text: str = ""
) -> None:
    """Flag a posted message as resume-awaiting. Idempotent; TTL-bounded."""
    try:
        mid = str(message_id).strip()
        tid = str(thread_id).strip()
        if not mid or not tid:
            return
        with _lock:
            data = _load()
            _prune(data)
            data[mid] = {"thread_id": tid, "text": text or "", "ts": time.time()}
            _save(data)
            logger.info("marked resume-awaiting: message %s -> thread %s", mid, tid)
    except Exception as e:  # never block a turn
        logger.debug("mark_resume_awaiting failed: %s", e)


def is_resume_awaiting(message_id: int | str) -> bool:
    """True if the message is currently flagged resume-awaiting (non-consuming)."""
    try:
        mid = str(message_id).strip()
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(mid)
            if not isinstance(entry, dict):
                return False
            return bool(entry.get("thread_id"))
    except Exception as e:  # never block a turn
        logger.debug("resume_registry is_resume_awaiting failed: %s", e)
        return False


def lookup(message_id: int | str) -> dict | None:
    """Return {thread_id, text} for a resume-awaiting message, or None.

    Consumption semantics: a successful lookup marks the entry consumed
    (decision 0.4 — an entry is pruned once consumed). TTL-expired entries are
    pruned opportunistically on lookup.
    """
    try:
        mid = str(message_id).strip()
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(mid)
            if not isinstance(entry, dict):
                return None
            data.pop(mid, None)  # consume
            _save(data)
            return {
                "thread_id": entry.get("thread_id"),
                "text": entry.get("text", ""),
            }
    except Exception as e:  # never block a turn
        logger.debug("resume_registry lookup failed: %s", e)
        return None
