"""Unit tests for the beta-deploy gate (B5/B6). GitHub calls are mocked."""
from __future__ import annotations

import app.beta_deploy as bd


# ── pure parsing ──

def test_parse_ship_target():
    assert bd.parse_ship_target("/ship dapp_beta#12") == ("dapp_beta", 12)
    assert bd.parse_ship_target("/ship dapp_beta 12") == ("dapp_beta", 12)
    assert bd.parse_ship_target("/ship") is None        # list mode
    # bare "#12" → default beta repo
    target = bd.parse_ship_target("/ship #7")
    assert target == (bd.beta_repos()[0], 7)


def test_parse_callback_data():
    assert bd.parse_callback_data("ship:dapp_beta:12") == ("ship", "dapp_beta", 12)
    assert bd.parse_callback_data("cancel") == ("cancel", None, None)
    assert bd.parse_callback_data("ship:dapp_beta") == ("ship", None, None)  # malformed → safe


def test_is_beta_repo(monkeypatch):
    monkeypatch.setattr(bd.settings, "beta_deploy_repos", ["dapp_beta"])
    assert bd.is_beta_repo("dapp_beta") is True
    assert bd.is_beta_repo("dapp_prod") is False   # prod is never shippable here
    assert bd.is_beta_repo("dapp") is False


def test_build_ship_keyboard():
    kb = bd.build_ship_keyboard("dapp_beta", 12)
    btns = kb["inline_keyboard"][0]
    assert btns[0]["callback_data"] == "ship:dapp_beta:12"
    assert btns[1]["callback_data"] == "cancel"


# ── ship_pr gating ──

def test_ship_pr_blocked_when_gate_disabled(monkeypatch):
    monkeypatch.setattr(bd.settings, "beta_deploy_gate_enabled", False)
    res = bd.ship_pr("dapp_beta", 12)
    assert res["ok"] is False and "disabled" in res["message"].lower()


def test_ship_pr_rejects_non_beta_repo(monkeypatch):
    monkeypatch.setattr(bd.settings, "beta_deploy_gate_enabled", True)
    monkeypatch.setattr(bd.settings, "beta_deploy_repos", ["dapp_beta"])
    res = bd.ship_pr("dapp_prod", 12)   # prod must never be shippable here
    assert res["ok"] is False and "not a beta repo" in res["message"].lower()


def test_ship_pr_refuses_when_ci_not_green(monkeypatch):
    monkeypatch.setattr(bd.settings, "beta_deploy_gate_enabled", True)
    monkeypatch.setattr(bd.settings, "beta_deploy_repos", ["dapp_beta"])
    monkeypatch.setattr(bd, "check_ci_green", lambda repo, pr: (False, "CI still running: build"))
    res = bd.ship_pr("dapp_beta", 12)
    assert res["ok"] is False and "not shipping" in res["message"].lower()


def test_ship_pr_merges_when_green(monkeypatch):
    monkeypatch.setattr(bd.settings, "beta_deploy_gate_enabled", True)
    monkeypatch.setattr(bd.settings, "beta_deploy_repos", ["dapp_beta"])
    monkeypatch.setattr(bd, "check_ci_green", lambda repo, pr: (True, "green"))

    class FakeGH:
        def merge_pr(self, repo, pr_number, merge_method="squash"):
            assert repo == "dapp_beta" and pr_number == 12 and merge_method == "squash"
            return {"merged": True, "sha": "abc123", "message": "merged"}

    monkeypatch.setattr(bd, "GitHubClient", FakeGH)
    res = bd.ship_pr("dapp_beta", 12)
    assert res["ok"] is True and res["sha"] == "abc123" and "Merged" in res["message"]
