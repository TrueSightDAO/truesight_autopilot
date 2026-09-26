"""Governor-only manual session-compaction trigger (PR1 of the context-compaction plan).

Plan of record: agentic_ai_context/plans/SOPHIA_CONTEXT_COMPACTION_PLAN.md, PR1
(manual trigger tool, de-risking step per invariant 8). This tool lets a governor
compact ONE named session's history on demand using the PR0 library
(``app/context_compaction.py``). Nothing here fires automatically — PR2 wires
automatic compaction into the turn path AFTER this tool has been validated
against real bloated sessions (plan step 1d checkpoint).

Safety properties (plan invariants 1-4, 7-8):
- **Full pre-compaction backup**: the on-disk session JSON is copied to a
  ``<hash>.pre-compact-<UTC>.json`` sibling BEFORE any rewrite, so a bad
  compaction is trivially reversible without re-deriving from the transcript
  repo (invariant 1).
- **Boundaries land on full turn boundaries only** — never mid-``tool_calls``/
  ``tool`` pair. The PR0 library's ``find_turn_boundaries`` guarantees this
  (invariant 3).
- **The most recent ``keep_last_turns`` turns stay verbatim** (invariant 4).
- **No racing an in-flight turn** (invariant 7): the tool refuses to compact a
  session that is currently mid-turn in another thread (``_live_progress`` /
  recently active ``_active_streams``), unless the target IS the calling session
  — whose per-session lock the running turn already holds.
- **Governor-only**: policy.py classifies ``compact_session_manual`` as WRITE,
  so non-governors are POLICY BLOCKed at the tool layer (mirrors ``ssh_run`` /
  ``deploy_autopilot``).

The handler is sync (tool-registry contract) and runs inside
``asyncio.to_thread(...)`` off the event loop, exactly like every other tool.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from ..config import settings
from ..context_compaction import (
    DEFAULT_KEEP_LAST_TURNS,
    DEFAULT_TOKEN_THRESHOLD,
    backup_session_file,
    compact_history,
    count_tokens,
)
from ..tool_registry import ToolSpec

logger = logging.getLogger("autopilot.tools.compact_session")

_ACTIVE_WINDOW_SECS = 300.0  # a session "active elsewhere" if seen in the last 5 min


def _resolve_session(
    session_ref: str, current_session_id: str | None
) -> tuple[str, str] | None:
    """Resolve a session reference to (session_key_or_hash, log_path).

    Accepts three forms:
      1. ``d32b2609056d`` — the 12-hex session-hash filename (as on disk).
      2. ``tg:-1003919341801:21264`` — the full session key (hashed to find file).
      3. ``21264`` — a bare numeric thread id within the current chat.

    Returns ``None`` when no on-disk session exists for the reference.
    """
    log_dir = settings.session_log_dir
    ref = (session_ref or "").strip()

    def _path_for(hash12: str) -> str | None:
        p = os.path.join(str(log_dir), f"{hash12}.json")
        return p if os.path.exists(p) else None

    # Form 3: bare numeric thread id -> current chat.
    if ref.isdigit() and current_session_id and current_session_id.startswith("tg:"):
        chat = current_session_id.split(":")[1] if ":" in current_session_id else ""
        if chat:
            key = f"tg:{chat}:{ref}"
            p = _path_for(hashlib.md5(key.encode()).hexdigest()[:12])
            if p:
                return key, p
    # Form 2: full session key (contains ':').
    if ":" in ref and "/" not in ref:
        p = _path_for(hashlib.md5(ref.encode()).hexdigest()[:12])
        if p:
            return ref, p
    # Form 1: 12-hex hash (optionally with .json suffix).
    stem = ref[:-5] if ref.endswith(".json") else ref
    if len(stem) == 12 and all(c in "0123456789abcdef" for c in stem.lower()):
        p = _path_for(stem.lower())
        if p:
            return stem.lower(), p
    return None


def _active_key_for_hash(target_hash: str) -> str | None:
    """Return a live session key (from main's activity pools) matching target_hash."""
    try:
        from app.main import _active_streams, _live_progress  # lazy: avoid import cycle
    except Exception:  # pragma: no cover — main not importable in some test contexts
        return None
    now = time.time()
    for key in list(_live_progress.keys()):
        try:
            if hashlib.md5(str(key).encode()).hexdigest()[:12] == target_hash:
                return str(key)
        except Exception:
            continue
    for key, ts in list(_active_streams.items()):
        try:
            if hashlib.md5(str(key).encode()).hexdigest()[:12] == target_hash:
                if now - float(ts or 0) < _ACTIVE_WINDOW_SECS:
                    return str(key)
        except Exception:
            continue
    return None


def _load_messages(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("full_history") or data.get("recent_messages") or []


def _save_messages(path: str, messages: list[dict], hash12: str) -> None:
    """Persist compacted history in the same JSON shape as main._log_session."""
    import datetime

    log_data = {
        "session_hash": hash12,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "message_count": len(messages),
        "full_history": messages,
    }
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
    # If the live in-memory session is loaded, keep it in sync.
    try:
        from app.main import _sessions  # lazy: avoid import cycle
    except Exception:
        _sessions = None
    if _sessions is not None:
        for key in list(_sessions.keys()):
            try:
                if hashlib.md5(str(key).encode()).hexdigest()[:12] == hash12:
                    _sessions[key] = messages
            except Exception:
                continue


def compact_session_manual(
    session: str,
    keep_last_turns: int = DEFAULT_KEEP_LAST_TURNS,
    token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
    force: bool = False,
    session_id: str | None = None,
    governor_name: str | None = None,
) -> str:
    """Compactly summarize one session's history. Returns a JSON string."""
    try:
        resolved = _resolve_session(session, session_id)
        if resolved is None:
            return json.dumps(
                {
                    "status": "error",
                    "reason": (
                        f"No session found for {session!r} under "
                        f"{settings.session_log_dir}. Pass a 12-hex session hash "
                        "(e.g. d32b2609056d), a full tg:<chat>:<thread> key, or a "
                        "numeric thread id in this chat."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        session_key, path = resolved
        hash12 = (
            os.path.basename(path)[: -len(".json")]
            if path.endswith(".json")
            else os.path.basename(path)
        )

        # Refuse to race an in-flight turn in another thread (invariant 7).
        active_key = _active_key_for_hash(hash12)
        if active_key is not None and active_key != (session_id or ""):
            return json.dumps(
                {
                    "status": "blocked",
                    "reason": (
                        f"Session {hash12} ({active_key}) is currently active in "
                        "another thread. Compaction must run when that thread is "
                        "idle, or on the calling session itself. Try again later."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )

        messages = _load_messages(path)
        if not messages:
            return json.dumps(
                {"status": "error", "reason": f"Session {hash12} has no history."},
                indent=2,
                ensure_ascii=False,
            )

        tokens_before = count_tokens(messages)
        msgs_before = len(messages)
        if tokens_before <= token_threshold and not force:
            return json.dumps(
                {
                    "status": "noop",
                    "session": hash12,
                    "reason": (
                        f"Session is {tokens_before} tokens (<= threshold "
                        f"{token_threshold}); nothing to compact. Pass force=true "
                        "to compact anyway."
                    ),
                    "messages": msgs_before,
                    "tokens": tokens_before,
                },
                indent=2,
                ensure_ascii=False,
            )

        # Full pre-compaction backup BEFORE any rewrite (invariant 1).
        backup_path = backup_session_file(path)
        if backup_path is None or not os.path.exists(str(backup_path)):
            return json.dumps(
                {
                    "status": "error",
                    "reason": f"Backup of {path} failed — aborting without compacting.",
                },
                indent=2,
                ensure_ascii=False,
            )

        compacted = compact_history(
            messages,
            keep_last_n_turns=int(keep_last_turns),
            # force=true must still fold: the library treats threshold<=0 as
            # DISABLED, so use 1 (any real session exceeds 1 token) instead of 0,
            # ensuring a huge user-supplied threshold cannot make force a no-op.
            token_threshold=1 if force else int(token_threshold),
        )
        tokens_after = count_tokens(compacted)
        _save_messages(path, compacted, hash12)

        reduction = (
            round((1 - tokens_after / tokens_before) * 100, 1) if tokens_before else 0.0
        )
        return json.dumps(
            {
                "status": "ok",
                "session": hash12,
                "backup": os.path.basename(str(backup_path)),
                "messages_before": msgs_before,
                "messages_after": len(compacted),
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "reduction_pct": reduction,
                "keep_last_turns": int(keep_last_turns),
                "token_threshold": int(token_threshold),
                "compacted_by": governor_name or "governor",
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001 — surface as JSON tool result, never crash the turn
        logger.exception("compact_session_manual failed")
        return json.dumps(
            {"status": "error", "reason": str(e)},
            indent=2,
            ensure_ascii=False,
        )


TOOL_SPEC = ToolSpec(
    name="compact_session_manual",
    description=(
        "Governor-only MANUAL session-context compaction (PR1 of the context-"
        "compaction plan). Compacts one named session's history: folds completed "
        "turns older than the last `keep_last_turns` into a single [CONTEXT "
        "SUMMARY] user message, reusing each turn's own 'Done this turn' report "
        "text. Takes a full pre-compaction backup (<hash>.pre-compact-*.json) "
        "before rewriting; keeps the most recent turns byte-identical; refuses "
        "to touch a session that is mid-turn in another thread. Pass a 12-hex "
        "session hash (e.g. d32b2609056d), a full tg:<chat>:<thread> key, or a "
        "numeric thread id in the current chat."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session": {
                "type": "string",
                "description": "Session to compact: 12-hex hash (d32b2609056d), "
                "full key (tg:-1003919341801:21264), or numeric thread id in "
                "the current chat.",
            },
            "keep_last_turns": {
                "type": "integer",
                "description": f"How many most-recent turns to keep verbatim (default {DEFAULT_KEEP_LAST_TURNS}).",
            },
            "token_threshold": {
                "type": "integer",
                "description": f"Compaction trigger: only act when token count exceeds this (default {DEFAULT_TOKEN_THRESHOLD}).",
            },
            "force": {
                "type": "boolean",
                "description": "Compact even if under the token threshold (default false).",
            },
        },
        "required": ["session"],
    },
    handler=lambda args, ctx: compact_session_manual(
        session=args.get("session", ""),
        keep_last_turns=args.get("keep_last_turns", DEFAULT_KEEP_LAST_TURNS),
        token_threshold=args.get("token_threshold", DEFAULT_TOKEN_THRESHOLD),
        force=bool(args.get("force", False)),
        session_id=ctx.get("session_id"),
        governor_name=ctx.get("governor_name"),
    ),
    default_roles=frozenset({"infrastructure"}),
)
