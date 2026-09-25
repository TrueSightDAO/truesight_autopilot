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


def mark_options(key: str, thread_id: int | str, options: list[str]) -> None:
    """Register an option set under an opaque ``key`` (multiple-choice buttons).

    Used by ``post_to_telegram_topic(options=[...])``: the caller generates a
    short token, embeds it in each button's ``callback_data``
    (``ro:<token>:<index>``), and registers the labels here. A button tap
    later recovers the labels via :func:`lookup_options` using only the token
    (Telegram caps callback_data at 64 bytes, so labels must live server-side).

    ``thread_id`` is recorded for context/introspection. Idempotent;
    TTL-bounded like every other entry.
    """
    try:
        k = str(key).strip()
        tid = str(thread_id).strip()
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not k or not tid or not opts:
            return
        with _lock:
            data = _load()
            _prune(data)
            entry = data.get(k)
            if not isinstance(entry, dict):
                entry = {"text": "", "ts": time.time()}
            entry["thread_id"] = tid
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
    single-use -- the entry is popped once its labels are read, so the same
    button menu cannot be answered twice. A plain resume-awaiting message
    (no ``options``) is left untouched and returns None.
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
        logger.debug("resume_registry lookup_options failed: %s", e)
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
