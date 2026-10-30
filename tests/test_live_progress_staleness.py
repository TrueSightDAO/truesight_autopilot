"""Regression: a leaked ``_live_progress`` entry must not block compaction forever.

``_live_progress`` is process-local with no TTL, and its pop sites sit only on the
success and explicit-cancel return paths -- no ``finally``. So a turn that dies
mid-way (exception, ``BaseException``, hard cancel) leaves its entry behind for the
life of the process. ``_active_key_for_hash`` used to return a key for *any*
presence, which meant one leaked entry permanently refused compaction for that
session hash. Shared state cannot have this bug: it carries a 7200s TTL, so the
defect was specifically in the local cache's lifecycle.
"""

import hashlib
import time

import pytest

from app.tools import compact_session_tool as cst


def _hash(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()[:12]


# --- _live_progress_is_fresh -------------------------------------------------


def test_fresh_heartbeat_is_live():
    now = time.time()
    assert cst._live_progress_is_fresh({"heartbeat": now, "started_at": now}, now) is True


def test_stale_heartbeat_is_not_live():
    now = time.time()
    stale = now - cst._LIVE_PROGRESS_MAX_AGE_SECS - 60
    assert cst._live_progress_is_fresh({"heartbeat": stale}, now) is False


def test_falls_back_to_started_at_without_heartbeat():
    now = time.time()
    assert cst._live_progress_is_fresh({"started_at": now}, now) is True
    old = now - cst._LIVE_PROGRESS_MAX_AGE_SECS - 1
    assert cst._live_progress_is_fresh({"started_at": old}, now) is False


def test_malformed_or_missing_record_is_not_live():
    now = time.time()
    assert cst._live_progress_is_fresh(None, now) is False
    assert cst._live_progress_is_fresh({}, now) is False
    assert cst._live_progress_is_fresh({"heartbeat": "garbage"}, now) is False
    assert cst._live_progress_is_fresh({"started_at": None}, now) is False


def test_window_is_longer_than_a_single_tool_round():
    """A long round must never be mistaken for a dead session."""
    assert cst._LIVE_PROGRESS_MAX_AGE_SECS >= 600.0


# --- end-to-end on the real guard -------------------------------------------
# These import app.main; skip cleanly where it is not importable (the guard
# itself already tolerates that, per its own docstring).


def _main_or_skip():
    try:
        from app import main
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - env dependent
        pytest.skip(f"app.main not importable here: {exc}")
    return main


def test_leaked_entry_no_longer_blocks_the_guard(monkeypatch):
    main = _main_or_skip()
    key = "tg:-1003919341801:21264"
    dead = {
        "round": 3,
        "started_at": time.time() - 86400,
        "heartbeat": time.time() - 86400,
    }
    monkeypatch.setattr(main, "_live_progress", {key: dead}, raising=False)
    monkeypatch.setattr(main, "_active_streams", {}, raising=False)
    assert cst._active_key_for_hash(_hash(key)) is None


def test_live_entry_still_blocks_the_guard(monkeypatch):
    """The guard must keep doing its actual job."""
    main = _main_or_skip()
    key = "tg:-1003919341801:21264"
    live = {"round": 3, "started_at": time.time(), "heartbeat": time.time()}
    monkeypatch.setattr(main, "_live_progress", {key: live}, raising=False)
    monkeypatch.setattr(main, "_active_streams", {}, raising=False)
    assert cst._active_key_for_hash(_hash(key)) == key
