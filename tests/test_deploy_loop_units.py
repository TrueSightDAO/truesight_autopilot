"""Guard (2026-10-31): deploy must INSTALL + RESTART the loop units.

Root cause this guards against: `scripts/deploy.sh` and `app/tools/deploy.py`
both installed units from an EXPLICIT list that omitted
`truesight-loop-daemon.service` and `truesight-loop-alerts.{service,timer}`,
and restarted only autopilot/telegram/discord/watchdog/vault.

So a merged, pulled loop fix never took effect: the daemon kept running the
PRE-FIX module for hours. Observed live 2026-10-30/31 -- #616 (supersession
guard) and #619 (consume-on-reply) were both merged and both on disk, yet
`truesight-loop-daemon` (PID 235450, started 12:58) still ran pre-fix code and
kept nagging `awaiting_stale`. Nothing in the deploy path would ever have
cycled it.

Invariants asserted here:
1. `_install_systemd_units` copies the loop-daemon unit and the alerts
   service+timer, and enables the timer.
2. The restart list includes `truesight-loop-daemon`.
3. `truesight-autopilot` stays LAST (2026-08-29 invariant: restarting it
   tears down its own process tree and aborts the rest of the list).
4. `scripts/deploy.sh` also copies + restarts the loop units (parity between
   the two deploy paths).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)

_REPO = Path(__file__).resolve().parents[1]


def _write_env(tmp_path, text: str) -> str:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_unit_install_includes_loop_units():
    captured: list[str] = []

    def _fake_run_local(command, cwd=None, timeout=60):
        captured.append(command)
        return ""

    orig = dep._run_local
    dep._run_local = _fake_run_local
    try:
        dep._install_systemd_units("/opt/truesight_autopilot", "sudo")
    finally:
        dep._run_local = orig

    assert captured, "expected a unit-install command"
    blob = " ".join(captured)
    assert "truesight-loop-daemon.service" in blob
    assert "truesight-loop-alerts.service" in blob
    assert "truesight-loop-alerts.timer" in blob
    assert "daemon-reload" in blob
    # timer must be enabled, or the 10-min alert sweep never runs
    assert re.search(r"enable\s+truesight-loop-alerts\.timer", blob)


@pytest.mark.parametrize("discord_enabled", [True, False])
def test_restart_list_includes_loop_daemon(monkeypatch, tmp_path, discord_enabled):
    monkeypatch.setattr(dep, "_get_current_commit", lambda rd: "abc1234")
    monkeypatch.setattr(dep, "_write_deploy_marker", lambda *a, **k: None)
    monkeypatch.setattr(dep, "_run_local", lambda *a, **k: "")
    monkeypatch.setattr(dep, "_run_nginx_certbot", lambda *a, **k: None)
    monkeypatch.setattr(dep, "_install_systemd_units", lambda rd, el: None)

    seen: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, *a, **k):
            seen.append(list(argv))

    monkeypatch.setattr(dep.subprocess, "Popen", _FakePopen)

    rd = _write_env(
        tmp_path, f"DISCORD_ADAPTER_ENABLED={'true' if discord_enabled else 'false'}\n"
    )
    dep._post_pull_steps(rd, 0.0, [])

    assert seen, "restart must fire"
    argv = " ".join(seen[-1])
    assert "truesight-loop-daemon" in argv
    # 2026-08-29 invariant: autopilot LAST
    assert seen[-1][-1] == "truesight-autopilot"


def test_deploy_sh_has_loop_unit_parity():
    """scripts/deploy.sh must install AND restart the loop units too."""
    sh = (_REPO / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "truesight-loop-daemon.service" in sh
    assert "truesight-loop-alerts.timer" in sh
    assert re.search(r"restart\s+truesight-loop-daemon", sh)
