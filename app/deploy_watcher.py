"""Safe deploy orchestration — idle-check watcher.

Monitors all active tracks (Telegram sessions, background loops, tool calls)
and only allows a service restart when all tracks are idle or have exceeded
their expected max duration. Prevents SIGTERM from killing mid-execution work
across parallel handoff threads.

See SOPHIA_MULTI_TENANT_GOVERNANCE_PLAN.md §13.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

STATE_DIR = Path(os.getenv("DEPLOY_WATCHER_STATE_DIR", "data"))
STATE_PATH = STATE_DIR / "active_tracks.json"

# Expected max durations per track type (seconds)
TRACK_TIMEOUTS: dict[str, int] = {
    "telegram_chat": 120,       # LLM call + tool execution
    "followup_monitor": 30,     # Probe + state write
    "email_poller": 15,         # Gmail query + dispatch
    "ssh_operation": 60,        # Clone, push, deploy
    "git_operation": 60,        # Branch, commit, push
    "aws_watcher": 10,          # Status check
    "daily_briefing": 60,       # LLM generation
    "deploy": 120,              # Pip install + restart
}

DEFAULT_TIMEOUT = 60  # fallback for unknown track types


# ── Track registry ─────────────────────────────────────────────────────────


def _state_path() -> Path:
    """Get the state file path, ensuring the directory exists."""
    path = STATE_PATH
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / str(STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state() -> dict[str, Any]:
    """Load the active tracks state file."""
    path = _state_path()
    if not path.exists():
        return {"version": 1, "tracks": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load active tracks state: %s", e)
        return {"version": 1, "tracks": []}


def _save_state(state: dict[str, Any]) -> None:
    """Save the active tracks state file atomically."""
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.rename(path)


def register_track(
    track_id: str,
    label: str,
    track_type: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Register a new active track.

    Args:
        track_id: Unique identifier (e.g. 'telegram:2744', 'followup-monitor').
        label: Human-readable description.
        track_type: One of the keys in TRACK_TIMEOUTS.
        metadata: Optional extra info (e.g. thread_id, session_id).
    """
    state = _load_state()
    now = _now_iso()

    # Remove any existing track with the same ID
    state["tracks"] = [t for t in state["tracks"] if t["id"] != track_id]

    state["tracks"].append({
        "id": track_id,
        "label": label,
        "track_type": track_type,
        "started_at": now,
        "last_heartbeat": now,
        "expected_max_duration_s": TRACK_TIMEOUTS.get(track_type, DEFAULT_TIMEOUT),
        "status": "running",
        "metadata": metadata or {},
    })
    _save_state(state)
    logger.debug("Track registered: %s (%s)", track_id, label)


def heartbeat(track_id: str) -> None:
    """Update the heartbeat timestamp for a running track.

    Call this on every iteration of a long-running loop so the deploy
    watcher can distinguish 'actively running' from 'crashed/stuck'.
    """
    state = _load_state()
    now = _now_iso()
    for track in state["tracks"]:
        if track["id"] == track_id and track["status"] == "running":
            track["last_heartbeat"] = now
            _save_state(state)
            return
    # Track not found — it may have been cleaned up. That's fine.
    logger.debug("Heartbeat for unknown track: %s", track_id)


def unregister_track(track_id: str, status: str = "completed") -> None:
    """Remove a track from the active registry.

    Args:
        track_id: The track to remove.
        status: Final status ('completed', 'aborted', 'crashed').
    """
    state = _load_state()
    state["tracks"] = [t for t in state["tracks"] if t["id"] != track_id]
    _save_state(state)
    logger.debug("Track unregistered: %s (%s)", track_id, status)


def get_active_tracks() -> list[dict[str, Any]]:
    """Get all currently registered tracks with status 'running'."""
    state = _load_state()
    return [t for t in state["tracks"] if t["status"] == "running"]


# ── Deploy gate ────────────────────────────────────────────────────────────


def can_deploy(*, force: bool = False) -> tuple[bool, list[dict[str, Any]]]:
    """Check if it's safe to restart the service.

    Args:
        force: If True, bypass all checks (manual override).

    Returns:
        Tuple of (can_deploy: bool, blocking_tracks: list).
        blocking_tracks contains tracks that are still actively running
        (within their expected max duration).
    """
    if force:
        logger.info("Deploy check: force=true, bypassing all checks.")
        return True, []

    tracks = get_active_tracks()
    now = time.time()
    blocking: list[dict[str, Any]] = []

    for track in tracks:
        elapsed = _seconds_since(track["last_heartbeat"])
        max_dur = track.get("expected_max_duration_s", DEFAULT_TIMEOUT)

        if elapsed < max_dur:
            # Track is actively running — blocks deploy
            blocking.append({
                "id": track["id"],
                "label": track["label"],
                "track_type": track["track_type"],
                "elapsed_s": round(elapsed, 1),
                "max_duration_s": max_dur,
                "reason": f"Active ({elapsed:.0f}s elapsed, {max_dur}s max)",
            })
            logger.info(
                "Deploy blocked by %s: %s (%ds elapsed, %ds max)",
                track["id"], track["label"], elapsed, max_dur,
            )
        else:
            # Track exceeded its max duration — treat as stuck/crashed
            logger.warning(
                "Deploy proceeding despite %s: exceeded max duration (%ds > %ds)",
                track["id"], elapsed, max_dur,
            )

    if blocking:
        return False, blocking

    logger.info("Deploy check: all tracks idle, safe to deploy.")
    return True, []


def get_system_status() -> dict[str, Any]:
    """Get a full system status snapshot for the vault web page."""
    tracks = get_active_tracks()
    now = time.time()

    enriched = []
    for track in tracks:
        elapsed = _seconds_since(track["last_heartbeat"])
        max_dur = track.get("expected_max_duration_s", DEFAULT_TIMEOUT)
        enriched.append({
            "id": track["id"],
            "label": track["label"],
            "track_type": track["track_type"],
            "elapsed_s": round(elapsed, 1),
            "max_duration_s": max_dur,
            "status": "active" if elapsed < max_dur else "stale",
            "started_at": track.get("started_at", ""),
            "last_heartbeat": track.get("last_heartbeat", ""),
            "metadata": track.get("metadata", {}),
        })

    can, blocking = can_deploy()

    return {
        "can_deploy": can,
        "blocking_tracks": blocking,
        "active_tracks": enriched,
        "total_tracks": len(tracks),
        "checked_at": _now_iso(),
    }


# ── Helpers ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _seconds_since(iso_timestamp: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, AttributeError):
        return float("inf")
