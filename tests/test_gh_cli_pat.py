"""Tests for app.gh_cli -- pinning the gh CLI to the canonical DAO PAT."""

from __future__ import annotations

import logging
import os

from app import gh_cli


class _Settings:
    def __init__(self, pat: str) -> None:
        self.github_pat = pat


def test_export_gh_token_sets_env(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert gh_cli.export_gh_token("tok123") == "exported"
    assert os.environ["GH_TOKEN"] == "tok123"


def test_export_gh_token_reports_already_set(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok123")
    assert gh_cli.export_gh_token("tok123") == "already-set"


def test_export_gh_token_empty_is_noop(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert gh_cli.export_gh_token("") == "no-token"
    assert os.environ.get("GH_TOKEN") is None


def test_reconcile_replaces_token_and_backs_up(tmp_path):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text(
        "github.com:\n"
        "    oauth_token: OLD_TOKEN\n"
        "    git_protocol: https\n"
        "    users:\n"
        "    - garyjob\n"
        "other.example.com:\n"
        "    oauth_token: KEEP_ME\n",
        encoding="utf-8",
    )
    assert gh_cli.reconcile_hosts_file(hosts, "NEW_TOKEN") == "reconciled"
    text = hosts.read_text(encoding="utf-8")
    assert "oauth_token: NEW_TOKEN" in text
    assert "OLD_TOKEN" not in text
    assert "oauth_token: KEEP_ME" in text  # a different host is untouched
    backups = list(tmp_path.glob("hosts.yml.bak.*"))
    assert len(backups) == 1
    assert "OLD_TOKEN" in backups[0].read_text(encoding="utf-8")


def test_reconcile_already_correct_writes_nothing(tmp_path):
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("github.com:\n    oauth_token: SAME\n", encoding="utf-8")
    assert gh_cli.reconcile_hosts_file(hosts, "SAME") == "already-correct"
    assert not list(tmp_path.glob("hosts.yml.bak.*"))


def test_reconcile_absent_file(tmp_path):
    assert gh_cli.reconcile_hosts_file(tmp_path / "nope.yml", "T") == "absent"


def test_no_token_value_in_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("github.com:\n    oauth_token: OLD\n", encoding="utf-8")
    monkeypatch.setattr(gh_cli, "gh_hosts_path", lambda: hosts)
    with caplog.at_level(logging.INFO):
        gh_cli.ensure_gh_cli_uses_canonical_pat(_Settings("SUPER_SECRET_TOKEN"))
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SUPER_SECRET_TOKEN" not in blob
    assert gh_cli._fingerprint("SUPER_SECRET_TOKEN") in blob


def test_empty_pat_leaves_everything_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    hosts = tmp_path / "hosts.yml"
    hosts.write_text("github.com:\n    oauth_token: KEEP\n", encoding="utf-8")
    monkeypatch.setattr(gh_cli, "gh_hosts_path", lambda: hosts)
    gh_cli.ensure_gh_cli_uses_canonical_pat(_Settings(""))
    assert "oauth_token: KEEP" in hosts.read_text(encoding="utf-8")
    assert os.environ.get("GH_TOKEN") is None
