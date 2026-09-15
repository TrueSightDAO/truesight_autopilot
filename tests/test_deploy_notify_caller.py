"""Deploy-notice + cooldown guard tests (2026-09-14).

Regression cover for the "deploy freezes the calling turn" bug: a deploy
restarts the very process serving the calling turn, so the turn dies before it
can report anything (Telegram: permanent "🔄 Thinking…" freeze).

Fix under test:
  * phase one parses the caller chat/thread out of the Telegram session key;
  * phase two posts a "deploying now" notice BEFORE the restart Popen;
  * a second deploy inside the cooldown window no-ops (no redundant restart).
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())
os.environ["DEPLOY_DRAIN_WAIT_SEC"] = "0"

try:
    import app.main as m
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def _fresh_epoch_file(monkeypatch, tmp_path):
    """Point the cooldown timestamp at a temp file so we never touch /tmp."""
    path = tmp_path / "last_deploy_epoch"
    monkeypatch.setattr(dep, "_LAST_DEPLOY_FILE", str(path), raising=False)
    return path


# ── _parse_caller_thread ──────────────────────────────────────────────────


def test_parse_caller_thread_telegram_keys():
    """Real Telegram session keys recover chat + thread."""
    chat, thread = dep._parse_caller_thread("ab12cd34ef56ab12cd34:tg:-1003919341801:29509")
    assert chat == -1003919341801
    assert thread == 29509
    # No-topic (thread 0 / absent) → thread_id None, chat intact.
    chat2, thread2 = dep._parse_caller_thread("ab12cd34ef56ab12cd34:tg:12345:0")
    assert chat2 == 12345
    assert thread2 is None


@pytest.mark.parametrize(
    "session",
    [None, "", "pubkeyonly", "deadbeefdeadbeefdead:dapp:tab-1", "x:tg:notanint:7"],
)
def test_parse_caller_thread_non_telegram_returns_none(session):
    """DApp/web/bogus sessions must never yield a Telegram target."""
    assert dep._parse_caller_thread(session) == (None, None)


# ── cooldown guard ────────────────────────────────────────────────────────


def test_deploy_recently_ran_round_trip(monkeypatch, tmp_path):
    _fresh_epoch_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPLOY_NOOP_COOLDOWN_SEC", "90")
    assert dep._deploy_recently_ran() is False  # no file yet
    dep._record_deploy_epoch()
    assert dep._deploy_recently_ran() is True


def test_deploy_recently_ran_respects_zero_cooldown(monkeypatch, tmp_path):
    _fresh_epoch_file(monkeypatch, tmp_path)
    dep._record_deploy_epoch()
    monkeypatch.setenv("DEPLOY_NOOP_COOLDOWN_SEC", "0")
    assert dep._deploy_recently_ran() is False


def test_deploy_recently_ran_expired_window(monkeypatch, tmp_path):
    path = _fresh_epoch_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPLOY_NOOP_COOLDOWN_SEC", "90")
    path.write_text(str(time.time() - 3600))
    assert dep._deploy_recently_ran() is False


def test_second_deploy_within_cooldown_noops(monkeypatch, tmp_path):
    """End-to-end: the guard short-circuits before any git/restart work."""
    _fresh_epoch_file(monkeypatch, tmp_path)
    monkeypatch.setenv("DEPLOY_NOOP_COOLDOWN_SEC", "90")
    monkeypatch.setattr(dep, "_is_local", lambda: True)
    dep._record_deploy_epoch()

    calls: list[str] = []

    def _boom(command, cwd=None, timeout=60):
        calls.append(command)
        raise AssertionError("guard must short-circuit before any command runs")

    monkeypatch.setattr(dep, "_run_local", _boom)
    out = json.loads(dep.deploy_autopilot(caller_session="k:tg:-100:1"))
    assert out["status"] == "noop"
    assert calls == []


# ── notice is sent before the restart ─────────────────────────────────────


def test_notify_deploy_starting_targets_caller_thread(monkeypatch):
    """_notify_deploy_starting passes the stashed chat/thread to the adapter."""
    monkeypatch.setenv("AUTOPILOT_DEPLOY_NOTIFY_CHAT_ID", "-1003919341801")
    monkeypatch.setenv("AUTOPILOT_DEPLOY_NOTIFY_THREAD_ID", "29509")

    seen: list[tuple] = []

    import app.telegram_adapter as ta

    monkeypatch.setattr(
        ta,
        "send_deploy_starting_notification",
        lambda c, t=None: seen.append((c, t)) or True,
        raising=False,
    )
    dep._notify_deploy_starting()
    assert seen == [(-1003919341801, 29509)]


def test_notify_deploy_starting_noop_without_target(monkeypatch):
    """No stashed chat (web session / manual call) → send nothing, don't crash."""
    monkeypatch.delenv("AUTOPILOT_DEPLOY_NOTIFY_CHAT_ID", raising=False)
    monkeypatch.delenv("AUTOPILOT_DEPLOY_NOTIFY_THREAD_ID", raising=False)
    dep._notify_deploy_starting()  # must be a silent no-op


def test_marker_carries_caller_thread(monkeypatch, tmp_path):
    """The marker persisted for the next boot carries chat/thread so the ✅
    completion notice lands in the originating topic."""
    marker = tmp_path / ".autopilot_deployed"
    monkeypatch.setattr(dep, "_MARKER_FILE", str(marker), raising=False)
    dep._write_deploy_marker("abc1234", 42.0, chat_id=-100, thread_id=9)
    data = json.loads(marker.read_text())
    assert data["chat_id"] == -100
    assert data["thread_id"] == 9
    assert data["commit"] == "abc1234"
