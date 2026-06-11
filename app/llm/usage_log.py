"""Usage logging — appends LLMUsage records to JSONL files.

Two write paths:
  Per-session (caller=chat): appends to SESSION_LOG_DIR/<sid>/usage.jsonl
  Per-worker (background): appends to usage/<date>/workers.jsonl in the transcript repo

Gated behind LLM_USAGE_LOG_ENABLED env var (default: off).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import LLMUsage

logger = logging.getLogger("autopilot.usage_log")

_ENABLED = os.getenv("LLM_USAGE_LOG_ENABLED", "").strip().lower() in ("1", "true", "yes")
SESSION_LOG_DIR = Path(os.getenv("SESSION_LOG_DIR", "/tmp/autopilot_sessions"))


def is_enabled() -> bool:
    return _ENABLED


def log_usage(
    provider: str,
    model: str,
    usage: LLMUsage,
    caller: str = "chat",
    session_id: str | None = None,
    turn: int | None = None,
    round_num: int = 1,
    latency_ms: int = 0,
    had_tool_calls: bool = False,
    finish_reason: str = "stop",
) -> None:
    """Append one usage record. No-op if logging is disabled."""
    if not _ENABLED:
        return

    est_usd = None  # computed by caller if available
    record: dict[str, Any] = {
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "provider": provider,
        "model": model,
        "caller": caller,
        "session_id": session_id,
        "turn": turn,
        "round": round_num,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
        "est_usd": est_usd,
        "latency_ms": latency_ms,
        "had_tool_calls": had_tool_calls,
        "finish_reason": finish_reason,
    }

    try:
        if caller == "chat" and session_id:
            _append_session(session_id, record)
        else:
            _append_worker(record)
    except Exception:
        logger.debug("Usage log write failed", exc_info=True)


def _append_session(session_id: str, record: dict[str, Any]) -> None:
    """Append to per-session usage.jsonl."""
    import hashlib

    sid_hash = hashlib.md5(session_id.encode()).hexdigest()[:12]
    log_dir = SESSION_LOG_DIR / sid_hash
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "usage.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _append_worker(record: dict[str, Any]) -> None:
    """Append to per-date workers.jsonl."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_dir = SESSION_LOG_DIR / "usage" / today
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "workers.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
