"""Persistent thread_id → Telegram forum-topic name cache (best-effort).

The Telegram Bot API doesn't expose a topic's name for an arbitrary existing topic,
and regular messages don't carry it — so we remember names as we see them:
  • when Sophia creates a topic (create_telegram_topic), and
  • when the adapter sees a forum_topic_created / forum_topic_edited service message.

Read by the vault system-status page so each active thread shows its name alongside
a clickable Telegram deep-link.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from .config import settings

logger = logging.getLogger("autopilot.topic_names")
_PATH = settings.session_log_dir / "_topic_names.json"
_lock = threading.Lock()


def _load() -> dict:
    try:
        if _PATH.is_file():
            return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def record_topic_name(thread_id, name) -> None:
    """Best-effort: remember the name for a forum thread. Idempotent."""
    try:
        tid = str(thread_id).strip()
        nm = (name or "").strip()
        if not tid or not nm:
            return
        with _lock:
            data = _load()
            if data.get(tid) == nm:
                return
            data[tid] = nm
            tmp = _PATH.with_name("_topic_names.json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, _PATH)
            logger.info("recorded topic name: thread %s = %r", tid, nm)
    except Exception as e:  # never block a turn
        logger.debug("record_topic_name failed: %s", e)


def get_topic_name(thread_id) -> str | None:
    return _load().get(str(thread_id).strip())
