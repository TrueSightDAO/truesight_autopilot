"""Per-host SSH identity resolution from the vault (vault migration).

Regression: ``ssh_run`` resolved ONE key for every host via the host-agnostic
``_key_path()``, which always returned the ``server_us`` key -- a key the
Nelanco hosts do not trust. Result: "Permission denied (publickey)" on any host
that trusts only ``ssh_key_nelanco_aws`` (seni_sk, seni_redis, ...).

Fix: ``FLEET[host]["vault_key"]`` names the vault credential each host trusts,
and ``_identity_for()`` resolves it (vault-first, with an on-box file fallback).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.tools import ssh_tools


def test_dao_protocol_pins_sophia_infra():
    """dao_protocol must declare its own key, not inherit the vault default."""
    assert ssh_tools.FLEET["dao_protocol"].get("key") == "~/.ssh/sophia_infra"


def test_nelanco_key_hosts_pin_the_nelanco_vault_key():
    for host in ("seni_sk", "seni_redis", "krake_sk", "getdata_redis", "seni_sql"):
        assert ssh_tools.FLEET[host].get("vault_key") == "ssh_key_nelanco_aws"


def test_server_us_hosts_pin_the_server_us_vault_key():
    for host in ("krake_ror", "krake_data"):
        assert ssh_tools.FLEET[host].get("vault_key") == "ssh_key_server_us"


def test_named_key_uses_vault_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ssh_tools, "_write_temp_key", lambda m: tmp_path / f"{m}.key")
    monkeypatch.setattr(
        ssh_tools,
        "_vault_credential",
        lambda n: "KEYMATERIAL" if n == "ssh_key_nelanco_aws" else None,
    )
    assert (
        ssh_tools._resolve_named_key("ssh_key_nelanco_aws")
        == tmp_path / "KEYMATERIAL.key"
    )


def test_named_key_falls_back_to_on_box_file(tmp_path, monkeypatch):
    keyfile = tmp_path / "NELANCO_aws_20201122.pem"
    keyfile.write_text("x")
    monkeypatch.setattr(ssh_tools, "_vault_credential", lambda n: None)
    monkeypatch.setattr(
        ssh_tools,
        "_VAULT_KEY_FILE_FALLBACK",
        {"ssh_key_nelanco_aws": str(keyfile)},
    )
    assert ssh_tools._resolve_named_key("ssh_key_nelanco_aws") == keyfile


def test_identity_for_nelanco_host_resolves_nelanco_key(tmp_path, monkeypatch):
    resolved = tmp_path / "nelanco.pem"
    resolved.write_text("x")
    monkeypatch.setattr(ssh_tools, "_resolve_by_vault_key", lambda h: resolved)
    assert ssh_tools._identity_for("seni_sk") == resolved


def test_identity_for_unpinned_host_uses_default(tmp_path, monkeypatch):
    default = tmp_path / "default.pem"
    default.write_text("x")
    # seni_ror is unpinned (trusts server_us) -> host-agnostic default.
    monkeypatch.setattr(ssh_tools, "_resolve_by_vault_key", lambda h: None)
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: default)
    assert ssh_tools._identity_for("seni_ror") == default


def test_pinned_key_wins_over_vault(tmp_path, monkeypatch):
    pinned = tmp_path / "sophia_infra"
    pinned.write_text("pinned")
    monkeypatch.setitem(ssh_tools.FLEET["dao_protocol"], "key", str(pinned))
    monkeypatch.setattr(
        ssh_tools, "_resolve_by_vault_key", lambda h: tmp_path / "not-used"
    )
    assert ssh_tools._identity_for("dao_protocol") == pinned


def test_missing_pinned_key_falls_back(tmp_path, monkeypatch):
    monkeypatch.setitem(ssh_tools.FLEET["dao_protocol"], "key", str(tmp_path / "gone"))
    default = tmp_path / "default.pem"
    default.write_text("x")
    monkeypatch.setattr(ssh_tools, "_resolve_by_vault_key", lambda h: None)
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: default)
    assert ssh_tools._identity_for("dao_protocol") == default


def test_vault_key_pin_skipped_when_unavailable(tmp_path, monkeypatch):
    """A pinned vault key that resolves to nothing must not hard-fail the host."""
    default = tmp_path / "default.pem"
    default.write_text("x")
    monkeypatch.setattr(ssh_tools, "_resolve_by_vault_key", lambda h: None)
    monkeypatch.setattr(ssh_tools, "_key_path", lambda: default)
    assert ssh_tools._identity_for("seni_redis") == default


def test_ssh_run_passes_nelanco_key_for_seni_redis(tmp_path, monkeypatch):
    """End-to-end shape: a Nelanco host carries the pinned key path in argv."""
    nelanco = tmp_path / "nelanco.pem"
    nelanco.write_text("x")
    monkeypatch.setattr(ssh_tools, "_resolve_by_vault_key", lambda h: nelanco)

    completed = MagicMock(returncode=0, stdout="hostname-ok\n", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        out = ssh_tools.ssh_run(host="seni_redis", command="hostname")

    assert out["status"] == "ok"
    argv = run.call_args[0][0]
    assert argv[argv.index("-i") + 1] == str(nelanco)
    assert "ubuntu@54.234.59.188" in argv
