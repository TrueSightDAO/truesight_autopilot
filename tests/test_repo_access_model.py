"""Unit tests for the default-allow repo-access model (PR2).

Covers ``Settings.repo_write_allowed`` / ``create_repo_allowed`` and the
``strict_repos`` / ``create_repo_patterns`` fields added by
plans/SOPHIA_REPO_ACCESS_DENYLIST_PLAN.md.

The governing invariant (§5d — zero behavior change until PR3 rewires the
guard sites): with no config, ALLOWED_REPOS-unset default-allow applies to
arbitrary repos, while the two protected classes stay refused.
"""

from __future__ import annotations

import os

from app.config import _DEFAULT_CREATE_REPO_PATTERNS, _repo_list, Settings


def test_default_is_default_allow_with_empty_strict_list():
    s = Settings()
    assert s.strict_repos == []
    ok, reason = s.repo_write_allowed("brand-new-repo")
    assert ok is True
    assert reason == ""


def test_default_allowed_repos_still_holds_the_legacy_literal_list():
    """PR2 must not shrink the legacy alias — several call sites read it
    directly until PR3 rewires them."""
    s = Settings()
    for repo in ("truesight_autopilot", "agentic_ai_context", "getdata-mcp-bridge"):
        assert repo in s.allowed_repos


def test_protected_classes_refused_in_default_allow_mode():
    s = Settings()
    ok, reason = s.repo_write_allowed("dapp_prod")
    assert ok is False
    assert "PRODUCTION repo" in reason
    # beta-first: the refusal names the beta replacement
    assert "dapp_beta" in reason

    ok, reason = s.repo_write_allowed("treasury-cache")
    assert ok is False
    assert "API-only data repo" in reason


def test_protected_classes_refused_even_if_explicitly_allowlisted():
    """The two classes outrank the allowlist in BOTH modes — a wide
    ALLOWED_REPOS can never re-open a prod or machine-owned-data write."""
    s = Settings(ALLOWED_REPOS="dapp_prod,treasury-cache,truesight_autopilot")
    assert s.repo_write_allowed("dapp_prod")[0] is False
    assert s.repo_write_allowed("treasury-cache")[0] is False
    assert s.repo_write_allowed("truesight_autopilot")[0] is True


def test_strict_mode_from_env_restricts_to_listed_repos():
    s = Settings(ALLOWED_REPOS="truesight_autopilot,agentic_ai_context")
    assert s.strict_repos == ["truesight_autopilot", "agentic_ai_context"]
    assert s.repo_write_allowed("agentic_ai_context")[0] is True
    ok, reason = s.repo_write_allowed("some-other-repo")
    assert ok is False
    assert "strict allowlist" in reason


def test_env_allowlist_mirrors_into_legacy_alias():
    s = Settings(ALLOWED_REPOS="a_one,b_two")
    assert s.allowed_repos == ["a_one", "b_two"]


def test_explicit_kwarg_allowlist_acts_as_strict_list():
    """Tests/orchestration_specs inject settings.allowed_repos directly; such
    an injected list must really gate (else the injection is meaningless)."""
    s = Settings(allowed_repos=["only_this_one"])
    assert s.strict_repos == ["only_this_one"]
    assert s.repo_write_allowed("only_this_one")[0] is True
    assert s.repo_write_allowed("something_else")[0] is False


def test_empty_allowlist_kwarg_stays_default_allow():
    s = Settings(allowed_repos=[])
    assert s.strict_repos == []
    assert s.repo_write_allowed("anything")[0] is True


def test_repo_write_allowed_blank_name():
    ok, reason = Settings().repo_write_allowed("")
    assert ok is False
    assert "required" in reason


# ── create_repo pattern gate ────────────────────────────────────────────────


def test_create_repo_default_patterns_match_blessed_shapes():
    s = Settings()
    for repo in ("fda-2026-program", "cfr-anapu", "pacaja-site"):
        assert s.create_repo_allowed(repo)[0] is True, repo


def test_create_repo_refuses_unblessed_name():
    ok, reason = Settings().create_repo_allowed("hallucinated-xyz")
    assert ok is False
    assert "pattern" in reason


def test_create_repo_env_override_replaces_defaults():
    s = Settings(CREATE_REPO_PATTERNS="my-*")
    assert s.create_repo_patterns == ["my-*"]
    assert s.create_repo_allowed("my-thing")[0] is True
    assert s.create_repo_allowed("cfr-anapu")[0] is False


def test_create_repo_blank_name():
    assert Settings().create_repo_allowed("")[0] is False


def test_default_pattern_list_is_nonempty():
    assert _DEFAULT_CREATE_REPO_PATTERNS


# ── env parsing helper ──────────────────────────────────────────────────────


def test_repo_list_accepts_commas_and_spaces(monkeypatch):
    monkeypatch.setenv("SOPHIA_TEST_CSV", " a_one , b_two  c_three ")
    assert _repo_list(os.environ.get("SOPHIA_TEST_CSV")) == [
        "a_one",
        "b_two",
        "c_three",
    ]


def test_repo_list_accepts_a_real_list():
    assert _repo_list(["a_one", "b_two"]) == ["a_one", "b_two"]


def test_repo_list_unset_is_empty(monkeypatch):
    monkeypatch.delenv("SOPHIA_TEST_CSV", raising=False)
    assert _repo_list(os.environ.get("SOPHIA_TEST_CSV")) == []


def test_repo_list_none_is_empty():
    assert _repo_list(None) == []
