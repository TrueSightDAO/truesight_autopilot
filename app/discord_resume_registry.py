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
    """Flag a posted message as resume-awaiting. Idempotent; TTL-bounded.

    A repeat mark with the same ``channel_id`` and ``text`` is a genuine no-op:
    it neither rewrites the JSON file nor refreshes ``ts``. Called on every
    Discord progress edit, so this is a hot path.
    """
    try:
        mid = str(message_id).strip()
        cid = str(channel_id).strip()
        if not mid or not cid or mid == "dry-run":
            return
        with _lock:
            data = _load()
            _prune(data)
            text = text or ""
            existing = data.get(mid)
            # True no-op when nothing changed (same channel, same text). The
            # Discord progress loop re-marks the SAME message on every edit,
            # and a blind _save() rewrote the file + logged on every tick
            # (observed 61 marks across just 7 message ids in ~66 min on
            # 2026-09-14). ts is refreshed only on a real change, so the TTL
            # means "last time this message actually changed".
            if (
                isinstance(existing, dict)
                and existing.get("channel_id") == cid
                and existing.get("text", "") == text
            ):
                return
            data[mid] = {"channel_id": cid, "text": text, "ts": time.time()}
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


def mark_options(key: str, channel_id: int | str, options: list[str]) -> None:
    """Register an option set under an opaque ``key`` (multiple-choice buttons).

    Discord twin of ``app.resume_registry.mark_options``. Used by
    ``post_to_discord_channel(options=[...])``: the caller generates a short
    token, embeds it in each button's ``custom_id`` (``ro:<token>:<index>``),
    and registers the labels here. A component tap later recovers the labels via
    :func:`lookup_options` using only the token, so the button payload stays
    tiny (Discord caps ``custom_id`` at 100 chars -- labels live server-side).

    ``channel_id`` is recorded for context/introspection. Idempotent;
    TTL-bounded like every other entry. NOTE: a key that is *also* a
    resume-awaiting message id is left alone -- mark_options never clobbers the
    ``{channel_id, text}`` resume fields an emoji-go would need.
    """
    try:
        k = str(key).strip()
        cid = str(channel_id).strip()
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not k or not cid or not opts:
            return
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(k)
            if not isinstance(entry, dict):
                entry = {"channel_id": cid, "text": "", "ts": time.time()}
            entry["channel_id"] = cid
            entry.setdefault("text", "")
            entry["options"] = opts
            entry["ts"] = time.time()
            data[k] = entry
            _save(data)
            logger.info("marked resume options: key %s -> %d option(s)", k, len(opts))
    except Exception as e:  # never block a turn
        logger.debug("mark_options failed: %s", e)


def lookup_options(key: str) -> list[str] | None:
    """Return the option labels registered under ``key``, or None.

    Consumption semantics (mirrors :func:`lookup`): an option set is
    single-use -- the labels are read and the whole entry popped, so the same
    button menu cannot be answered twice. A plain resume-awaiting message (no
    ``options``) returns None and is left untouched.
    """
    try:
        k = str(key).strip()
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(k)
            if not isinstance(entry, dict):
                return None
            opts = entry.get("options")
            if not isinstance(opts, list) or not opts:
                return None
            data.pop(k, None)  # consume
            _save(data)
            return [str(o) for o in opts]
    except Exception as e:  # never block a turn
        logger.debug("lookup_options failed: %s", e)
        return None


def peek_options(key: str) -> list[str] | None:
    """Non-consuming read of an option set (used for token-collision checks).

    Unlike :func:`lookup_options` this leaves the entry in place, so it is safe
    to call when generating a new menu token.
    """
    try:
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(str(key).strip())
            opts = entry.get("options") if isinstance(entry, dict) else None
            if isinstance(opts, list) and opts:
                return [str(o) for o in opts]
            return None
    except Exception as e:  # never block a turn
        logger.debug("peek_options failed: %s", e)
        return None
