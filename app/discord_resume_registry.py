"""Discord resume-awaiting registry \u2014 message_id -> {channel_id, text}.

Discord twin of ``app/resume_registry.py`` (which serves Telegram). Deliberately
a SEPARATE file + lock: the Discord adapter runs in its own process
(``truesight-autopilot-discord.service``), so a shared file would be a
cross-process write race \u2014 the class of bug that bricked Telegram threads 3 and
780. Only the Discord adapter reads/writes this file.

Flow (Tier-2 parity #6, reaction-based go-signal):
  * After Sophia posts a reply in a channel, she flags that message's id as
    resume-awaiting (see the ``discord_resume_registry.mark_resume_awaiting``
    hooks on the reply paths in ``app/discord_adapter.py``).
  * Later (minutes to days), a governor reacts with a standard emoji to that
    message. ``handle_message_reaction`` looks the message_id up here to recover
    the channel + the resume text, exactly as a typed go-signal would carry them.

Entries are persisted to a small JSON file next to ``_topic_names.json`` so the
registry survives an adapter restart between the post and the reaction. Bounded:
entries are pruned once consumed or after a TTL (default 7 days).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from .config import settings

logger = logging.getLogger("autopilot.discord_resume_registry")

_PATH = settings.session_log_dir / "_discord_resume_awaiting.json"
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
        tmp = _PATH.with_name("_discord_resume_awaiting.json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception as e:  # never block a turn
        logger.debug("discord_resume_registry save failed: %s", e)


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


def mark_resume_awaiting(
    message_id: int | str, channel_id: int | str, text: str = ""
) -> None:
    """Flag a posted message as resume-awaiting. Idempotent; TTL-bounded."""
    try:
        mid = str(message_id).strip()
        cid = str(channel_id).strip()
        if not mid or not cid or mid == "dry-run":
            return
        with _lock:
            data = _load()
            _prune(data)
            data[mid] = {"channel_id": cid, "text": text or "", "ts": time.time()}
            _save(data)
            logger.info("marked resume-awaiting: message %s -> channel %s", mid, cid)
    except Exception as e:  # never block a turn
        logger.debug("mark_resume_awaiting failed: %s", e)


def is_resume_awaiting(message_id: int | str) -> bool:
    """True if the message is currently flagged resume-awaiting."""
    try:
        mid = str(message_id).strip()
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(mid)
            if not isinstance(entry, dict):
                return False
            return bool(entry.get("channel_id"))
    except Exception as e:  # never block a turn
        logger.debug("is_resume_awaiting failed: %s", e)
        return False


def lookup(message_id: int | str) -> dict | None:
    """Return {channel_id, text} for a resume-awaiting message, or None.

    Consumption semantics: a successful lookup marks the entry consumed so a
    single reaction cannot trigger two turns.
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
                "channel_id": entry.get("channel_id"),
                "text": entry.get("text", ""),
            }
    except Exception as e:  # never block a turn
        logger.debug("lookup failed: %s", e)
        return None
