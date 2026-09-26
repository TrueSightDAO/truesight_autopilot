"""Tests for scripts/fleet_probe.py -- the vault-native cron probe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.fleet_probe as fleet_probe  # noqa: E402


def _patch_ssh_run(monkeypatch, fake):
    import app.tools.ssh_tools as ssh_tools

    monkeypatch.setattr(ssh_tools, "ssh_run", fake)


def test_unknown_host_is_local_error(monkeypatch, capsys):
    _patch_ssh_run(
        monkeypatch, lambda *a, **k: pytest.fail("ssh_run must not be called")
    )
    rc = fleet_probe.main(["--host", "no_such_host", "--command", "df /"])
    assert rc == 3
    assert "unknown host" in capsys.readouterr().err


def test_ok_prints_stdout_and_zero(monkeypatch, capsys):
    calls = {}

    def fake(host, command, timeout_secs=15):
        calls.update(host=host, command=command, timeout_secs=timeout_secs)
        return {"status": "ok", "stdout": "42\n", "returncode": 0}

    _patch_ssh_run(monkeypatch, fake)
    rc = fleet_probe.main(
        ["--host", "krake_data", "--command", "df /", "--timeout", "9"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "42"
    assert calls == {"host": "krake_data", "command": "df /", "timeout_secs": 9}


def test_nonzero_exit_propagates_rc(monkeypatch, capsys):
    _patch_ssh_run(
        monkeypatch,
        lambda *a, **k: {"status": "nonzero_exit", "stdout": "0\n", "returncode": 7},
    )
    rc = fleet_probe.main(["--host", "krake_data", "--command", "df /"])
    assert rc == 7
    assert capsys.readouterr().out.strip() == "0"


def test_error_status_is_local_error(monkeypatch, capsys):
    _patch_ssh_run(
        monkeypatch,
        lambda *a, **k: {"status": "error", "reason": "command timed out after 15s"},
    )
    rc = fleet_probe.main(["--host", "krake_data", "--command", "df /"])
    assert rc == 3
    assert "timed out" in capsys.readouterr().err


def test_real_fleet_host_resolves_identity(monkeypatch):
    """A probe against a real host must reach ssh_run (identity resolution path)."""
    captured = {}

    def fake(host, command, timeout_secs=15):
        captured["host"] = host
        return {"status": "ok", "stdout": "hostname\n", "returncode": 0}

    _patch_ssh_run(monkeypatch, fake)
    assert fleet_probe.main(["--host", "dao_protocol", "--command", "hostname"]) == 0
    assert captured["host"] == "dao_protocol"


def test_df_alert_scripts_have_no_hardcoded_key_paths():
    """The df-alert cron scripts must not pin bare key files again.

    Archiving the bare PEMs (migration plan Unit 6) must not break the disk
    alerts, so these scripts must resolve credentials via fleet_probe.py only.
    """
    scripts_dir = REPO_ROOT / "scripts"
    # df-alert.sh probes THIS box only (no SSH, no key) -- it needs no probe
    # helper, but must still carry no key path. The fleet scripts must use it.
    remote = ["df-alert-fleet.sh", "df-alert-remote-krakedata.sh"]
    for name in ["df-alert.sh", *remote]:
        text = (scripts_dir / name).read_text()
        assert ".pem" not in text, f"{name} hardcodes a .pem path"
        assert "/home/ubuntu" not in text, f"{name} hardcodes a /home/ubuntu path"
    for name in remote:
        text = (scripts_dir / name).read_text()
        assert "fleet_probe.py" in text, f"{name} must probe via fleet_probe.py"


def test_df_alert_scripts_are_installed_by_deploy():
    """deploy.sh must install the versioned df-alert scripts to /usr/local/bin."""
    deploy = (REPO_ROOT / "scripts" / "deploy.sh").read_text()
    for name in ["df-alert.sh", "df-alert-fleet.sh", "df-alert-remote-krakedata.sh"]:
        assert (
            f"install -m 755 $REMOTE_DIR/scripts/{name} /usr/local/bin/{name}" in deploy
        )
