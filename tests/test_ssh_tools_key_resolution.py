"""Regression tests for per-host SSH identity resolution (2026-09-13).

Bug: ``ssh_run`` resolved ONE key for every host via the vault-first
``_key_path()``, which returned ``ssh_key_server_us`` (the Krake key) — a key
``dao_protocol`` does not trust. Result: "Permission denied (publickey)" on
dao_protocol even though ``~/.ssh/sophia_infra`` works from the same box.

Fix: a host may pin its own key via ``FLEET[host]["key"]``. Hosts without a
pinned key keep the historical vault-first behaviour, so no other host's
resolution changes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.tools import ssh_tools


def test_dao_protocol_pins_sophia_infra():
    """dao_protocol must declare its own key, not inherit the vault default."""
    assert ssh_tools.FLEET["dao_protocol"].get("key") == "~/.ssh/sophia_infra"


def test_pinned_key_wins_over_vault_default(tmp_path, monkeypatch):
    """A pinned host uses its own key even when a vault key is available."""
    pinned = tmp_path / "sophia_infra"
    pinned.write_text("pinned-key")
    monkeypatch.setitem(ssh_tools.FLEET["dao_protocol"], "key", str(pinned))

    # The vault-first default would return this — the source of the bug.
    wrong = tmp_path / "server_us.pem"
    wrong.write_text("wrong-key")
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: wrong)

    assert ssh_tools._identity_for("dao_protocol") == pinned


def test_unpinned_host_falls_back_to_default(tmp_path, monkeypatch):
    """Hosts without a pinned key keep the historical vault-first behaviour."""
    default = tmp_path / "default.pem"
    default.write_text("default-key")
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: default)
    assert ssh_tools._identity_for("seni_ror") == default


def test_missing_pinned_key_falls_back(tmp_path, monkeypatch):
    """A pinned key that vanished must not hard-fail — fall back."""
    monkeypatch.setitem(ssh_tools.FLEET["dao_protocol"], "key", str(tmp_path / "gone"))
    default = tmp_path / "default.pem"
    default.write_text("default-key")
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: default)
    assert ssh_tools._identity_for("dao_protocol") == default


def test_ssh_run_passes_pinned_key_for_dao_protocol(tmp_path, monkeypatch):
    """End-to-end shape: the ssh argv must carry the pinned -i path."""
    pinned = tmp_path / "sophia_infra"
    pinned.write_text("pinned-key")
    monkeypatch.setitem(ssh_tools.FLEET["dao_protocol"], "key", str(pinned))

    completed = MagicMock(returncode=0, stdout="hostname-ok\n", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        out = ssh_tools.ssh_run(host="dao_protocol", command="hostname")

    assert out["status"] == "ok"
    argv = run.call_args[0][0]
    assert "-i" in argv
    assert argv[argv.index("-i") + 1] == str(pinned)
    assert "ubuntu@98.93.94.86" in argv
