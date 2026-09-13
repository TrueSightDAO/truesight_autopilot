"""Guard (2026-09-13): deploy_autopilot must actually INSTALL + gate the Discord
adapter systemd unit.

Root cause this guards against: the deploy path (both scripts/deploy.sh and
app/tools/deploy.py) installed systemd units from an EXPLICIT list that omitted
truesight-autopilot-discord.service, and deploy.py installed no units at all.
So the Discord adapter could be merged + pulled to the box and still never
exist as a unit -- deploy-dark would silently no-op.

Second invariant: the adapter raises SystemExit when DISCORD_ADAPTER_ENABLED is
unset, and its unit is Restart=always. Installing it is safe; STARTING it
unconditionally is not (restart loop). So the restart list must include it only
when the flag is true.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def _write_env(tmp_path, text: str) -> str:
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return str(tmp_path)


def test_unit_install_command_includes_discord():
    """_install_systemd_units must copy the discord unit + daemon-reload."""
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
    assert "truesight-autopilot-discord.service" in blob
    assert "truesight-autopilot-watchdog.service" in blob
    assert "daemon-reload" in blob


def test_discord_unit_enabled_true(tmp_path):
    rd = _write_env(tmp_path, "FOO=bar\nDISCORD_ADAPTER_ENABLED=true\n")
    assert dep._discord_unit_enabled(rd) is True


def test_discord_unit_enabled_false_and_missing(tmp_path):
    rd = _write_env(tmp_path, "DISCORD_ADAPTER_ENABLED=false\n")
    assert dep._discord_unit_enabled(rd) is False
    # missing file -> fail closed
    assert dep._discord_unit_enabled(str(tmp_path / "nope")) is False


def test_discord_quoted_true(tmp_path):
    rd = _write_env(tmp_path, 'DISCORD_ADAPTER_ENABLED="true"\n')
    assert dep._discord_unit_enabled(rd) is True


def test_local_restart_list_gated_on_flag(monkeypatch, tmp_path):
    """The local restart list must include the discord unit ONLY when enabled."""
    monkeypatch.setattr(dep, "_get_current_commit", lambda rd: "abc1234")
    monkeypatch.setattr(dep, "_write_deploy_marker", lambda *a, **k: None)
    monkeypatch.setattr(dep, "_run_local", lambda *a, **k: "")
    monkeypatch.setattr(dep, "_run_nginx_certbot", lambda *a, **k: None)

    seen: list[list[str]] = []

    class _FakePopen:
        def __init__(self, argv, *a, **k):
            seen.append(list(argv))

    monkeypatch.setattr(dep.subprocess, "Popen", _FakePopen)

    # --- enabled ---
    rd = _write_env(tmp_path, "DISCORD_ADAPTER_ENABLED=true\n")
    dep._post_pull_steps(rd, 0.0, [])
    assert seen, "restart must fire"
    enabled_argv = " ".join(seen[-1])
    assert "truesight-autopilot-discord" in enabled_argv
    # autopilot must remain LAST (2026-08-29 invariant)
    assert seen[-1][-1] == "truesight-autopilot"

    # --- disabled ---
    (tmp_path / "b").mkdir(exist_ok=True)
    rd2 = _write_env(tmp_path / "b", "DISCORD_ADAPTER_ENABLED=false\n")
    seen.clear()
    dep._post_pull_steps(rd2, 0.0, [])
    assert seen, "restart must still fire when discord is disabled"
    disabled_argv = " ".join(seen[-1])
    assert "truesight-autopilot-discord" not in disabled_argv
    assert seen[-1][-1] == "truesight-autopilot"


def test_install_units_step_recorded(monkeypatch, tmp_path):
    """The deploy step list must record install_units before restart_service."""
    monkeypatch.setattr(dep, "_get_current_commit", lambda rd: "abc1234")
    monkeypatch.setattr(dep, "_write_deploy_marker", lambda *a, **k: None)
    monkeypatch.setattr(dep, "_run_local", lambda *a, **k: "")
    monkeypatch.setattr(dep, "_run_nginx_certbot", lambda *a, **k: None)
    monkeypatch.setattr(dep, "_install_systemd_units", lambda rd, el: None)

    class _FakePopen:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(dep.subprocess, "Popen", _FakePopen)
    rd = _write_env(tmp_path, "DISCORD_ADAPTER_ENABLED=true\n")
    out = __import__("json").loads(dep._post_pull_steps(rd, 0.0, []))
    names = [s["step"] for s in out["steps"]]
    assert "install_units" in names
    assert names.index("install_units") < names.index("restart_service")
