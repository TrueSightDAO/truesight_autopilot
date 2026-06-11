"""Unit tests for ssh_run — registry gate, key check, dispatch shape."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.tools import ssh_tools


def test_unknown_host_rejected_with_fleet_listing():
    out = ssh_tools.ssh_run(host="not-a-host", command="uptime")
    assert out["status"] == "error"
    assert "unknown host" in out["reason"]
    assert "seni_ror" in out["fleet"]


def test_missing_key_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHIA_SSH_KEY_PATH", str(tmp_path / "nope"))
    # _key_path() falls through to real keys on the autopilot box; mock it
    # to return the non-existent path so the test is hermetic.
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: tmp_path / "nope")
    out = ssh_tools.ssh_run(host="seni_ror", command="uptime")
    assert out["status"] == "error"
    assert "No SSH key found" in out["reason"]


def test_dispatch_builds_correct_ssh_command(tmp_path, monkeypatch):
    key = tmp_path / "sophia_infra"
    key.write_text("fake-key")
    monkeypatch.setenv("SOPHIA_SSH_KEY_PATH", str(key))

    completed = MagicMock(returncode=0, stdout="up 12 days\n", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        out = ssh_tools.ssh_run(host="seni_ror", command="uptime", timeout_secs=30)

    assert out["status"] == "ok"
    assert out["returncode"] == 0
    assert out["stdout"] == "up 12 days\n"
    assert out["ip"] == "54.211.179.126"

    argv = run.call_args[0][0]
    assert argv[0] == "ssh"
    assert "ubuntu@54.211.179.126" in argv
    assert "BatchMode=yes" in " ".join(argv)
    assert argv[-1] == "uptime"
    assert run.call_args[1]["timeout"] == 30


def test_nonzero_exit_reported(tmp_path, monkeypatch):
    key = tmp_path / "sophia_infra"
    key.write_text("fake-key")
    monkeypatch.setenv("SOPHIA_SSH_KEY_PATH", str(key))

    completed = MagicMock(returncode=1, stdout="", stderr="no such unit\n")
    with patch("subprocess.run", return_value=completed):
        out = ssh_tools.ssh_run(host="krake_nginx", command="systemctl status ghost")

    assert out["status"] == "nonzero_exit"
    assert out["stderr"] == "no such unit\n"


def test_tool_spec_registered_and_sre_gated():
    from app.tool_registry import get_registry, reset_registry_for_tests

    reset_registry_for_tests()
    spec = get_registry().get("ssh_run")
    assert spec is not None
    assert spec.default_roles == frozenset({"infrastructure"})
    assert sorted(ssh_tools.FLEET.keys()) == spec.parameters["properties"]["host"]["enum"]
