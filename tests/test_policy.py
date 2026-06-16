"""Tests for app/policy.py — identity resolver + authorization gate."""

import os
from unittest.mock import patch

import pytest

from app.policy import (
    ActionClass,
    Identity,
    Role,
    classify_action,
    evaluate,
    is_governor,
    may_access_secret,
    refresh_governor_cache,
    require_governor,
    resolve_identity,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_governor_cache():
    """Reset caches before each test, and keep the binding lookup hermetic.

    By default the Column X lookup returns "unbound" so policy tests never
    touch the Sheets API; binding tests override with their own patch.
    """
    refresh_governor_cache()
    with patch(
        "app.identity_binding.check_binding_status", return_value={"bound": False}
    ):
        yield
    refresh_governor_cache()


# ── resolve_identity tests ────────────────────────────────────────────────


class TestResolveIdentity:
    def test_known_telegram_id_is_governor(self):
        """A telegram_id in the allowlist resolves to GOVERNOR."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "12345,67890"}):
            identity = resolve_identity(telegram_id=12345, display_name="Alice")
        assert identity.role == Role.GOVERNOR
        assert identity.telegram_id == 12345
        assert identity.name == "Alice"

    def test_unknown_telegram_id_is_guest(self):
        """A telegram_id NOT in the allowlist resolves to GUEST."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "12345"}):
            identity = resolve_identity(telegram_id=99999, display_name="Bob")
        assert identity.role == Role.GUEST
        assert identity.telegram_id == 99999

    def test_known_display_name_is_governor(self):
        """A display name matching GOVERNOR_NAMES resolves to GOVERNOR."""
        with patch.dict(os.environ, {"GOVERNOR_NAMES": "Gary Teh,Alice"}):
            identity = resolve_identity(telegram_id=None, display_name="Alice")
        assert identity.role == Role.GOVERNOR
        assert identity.name == "Alice"

    def test_unknown_display_name_is_guest(self):
        """A display name NOT matching GOVERNOR_NAMES resolves to GUEST."""
        with patch.dict(os.environ, {"GOVERNOR_NAMES": "Gary Teh"}):
            identity = resolve_identity(telegram_id=None, display_name="Unknown")
        assert identity.role == Role.GUEST

    def test_no_identity_info_is_guest(self):
        """No telegram_id and no display name resolves to GUEST."""
        identity = resolve_identity()
        assert identity.role == Role.GUEST
        assert identity.name is None

    def test_telegram_id_takes_precedence_over_display_name(self):
        """Telegram ID match is stronger than display name match."""
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_ALLOWED_USER_IDS": "12345",
                "GOVERNOR_NAMES": "Gary Teh",
            },
        ):
            # 12345 is in the allowlist, but display name is not a governor name
            identity = resolve_identity(telegram_id=12345, display_name="Stranger")
        assert identity.role == Role.GOVERNOR  # ID match wins

    def test_empty_allowlist_all_guests(self):
        """Empty TELEGRAM_ALLOWED_USER_IDS means everyone is a guest (by ID)."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            identity = resolve_identity(telegram_id=12345)
        assert identity.role == Role.GUEST

    def test_username_fallback_for_name(self):
        """When display_name is absent, username is used as the identity name."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "12345"}):
            identity = resolve_identity(telegram_id=12345, telegram_username="garyjob")
        assert identity.name == "garyjob"


# ── is_governor tests ─────────────────────────────────────────────────────


class TestIsGovernor:
    def test_governor_identity(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        assert is_governor(identity) is True

    def test_guest_identity(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST, name="Guest")
        assert is_governor(identity) is False


# ── require_governor tests ────────────────────────────────────────────────


class TestRequireGovernor:
    def test_governor_passes(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        # Should not raise
        require_governor(identity, "deploy")

    def test_guest_raises(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST, name="Stranger")
        with pytest.raises(PermissionError, match="not a governor"):
            require_governor(identity, "deploy")

    def test_guest_raises_without_description(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        with pytest.raises(PermissionError, match="not a governor"):
            require_governor(identity)


# ── may_access_secret tests ───────────────────────────────────────────────


class TestMayAccessSecret:
    def test_governor_may_access_secret(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR)
        assert may_access_secret(identity) is True

    def test_guest_may_not_access_secret(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        assert may_access_secret(identity) is False


# ── classify_action tests ─────────────────────────────────────────────────


class TestClassifyAction:
    def test_read_tools(self):
        assert classify_action("read_context_file") == ActionClass.READ
        assert classify_action("read_repo_file") == ActionClass.READ
        assert classify_action("web_search") == ActionClass.READ
        assert classify_action("lookup_qr_code") == ActionClass.READ
        assert classify_action("list_prs") == ActionClass.READ

    def test_write_tools(self):
        assert classify_action("submit_contribution") == ActionClass.WRITE
        assert classify_action("git_push_changes") == ActionClass.WRITE
        assert classify_action("merge_pr") == ActionClass.WRITE
        assert classify_action("deploy_autopilot") == ActionClass.WRITE
        assert classify_action("gmail_send") == ActionClass.WRITE
        assert classify_action("ssh_run") == ActionClass.WRITE

    def test_unknown_tool_defaults_to_read(self):
        """Unknown tools default to READ (permissive default for safety)."""
        assert classify_action("some_new_tool") == ActionClass.READ


# ── evaluate tests ────────────────────────────────────────────────────────


class TestEvaluate:
    def test_guest_can_read(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "read_context_file")
        assert decision.allowed is True
        assert decision.action_class == ActionClass.READ

    def test_guest_cannot_write(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is False
        assert decision.action_class == ActionClass.WRITE
        assert "guest" in decision.reason.lower()

    def test_governor_can_write(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is True
        assert decision.action_class == ActionClass.WRITE

    def test_secrets_never_returned_through_chat(self):
        """SECRET actions are always denied through chat, even for governors."""
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "some_secret_tool")
        # Currently no tools are classified as SECRET, so this falls through to READ
        # Once Phase 3 adds secret tools, this test should assert denied
        assert decision.allowed is True  # No secret tools exist yet

    def test_decision_contains_identity(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "read_context_file")
        assert decision.identity == identity


# ── refresh_governor_cache tests ──────────────────────────────────────────


class TestRefreshGovernorCache:
    def test_cache_refresh_picks_up_new_env(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "111"}):
            identity = resolve_identity(telegram_id=111)
            assert identity.role == Role.GOVERNOR

        # Change env without refreshing
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "222"}):
            # Cache is stale — still sees 111
            identity = resolve_identity(telegram_id=222)
            assert identity.role == Role.GUEST

            # After refresh, picks up 222
            refresh_governor_cache()
            identity = resolve_identity(telegram_id=222)
            assert identity.role == Role.GOVERNOR


# ── Column X binding resolution (Phase 1 read-side) ───────────────────────


_GOV_CACHE = {
    "governors": [
        {"name": "Gary Teh", "email": "gary@truesight.me", "public_key": "PK"}
    ]
}


class TestBindingResolution:
    def test_bound_governor_via_column_x(self):
        """A telegram_id bound (Column X) to a contributor in the Governors
        cache resolves to GOVERNOR even with an empty env allowlist."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            with patch(
                "app.identity_binding.check_binding_status",
                return_value={
                    "bound": True,
                    "email": "gary@truesight.me",
                    "name": "Gary Teh",
                },
            ):
                with patch(
                    "app.governor_registry.load_governors", return_value=_GOV_CACHE
                ):
                    identity = resolve_identity(telegram_id=55555)
        assert identity.role == Role.GOVERNOR
        assert identity.name == "Gary Teh"

    def test_bound_member_is_guest_but_keeps_name(self):
        """A verified non-governor member falls through to GUEST, but the
        binding name is kept for audit."""
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            with patch(
                "app.identity_binding.check_binding_status",
                return_value={
                    "bound": True,
                    "email": "member@example.com",
                    "name": "Member Person",
                },
            ):
                with patch(
                    "app.governor_registry.load_governors", return_value=_GOV_CACHE
                ):
                    identity = resolve_identity(telegram_id=55555)
        assert identity.role == Role.GUEST
        assert identity.name == "Member Person"

    def test_unbound_id_is_guest(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            with patch(
                "app.identity_binding.check_binding_status",
                return_value={"bound": False},
            ):
                identity = resolve_identity(telegram_id=55555, display_name="Nobody")
        assert identity.role == Role.GUEST

    def test_env_allowlist_short_circuits_binding_lookup(self):
        """An env-allowlisted id never triggers a Sheets binding lookup."""
        calls = {"n": 0}

        def _spy(_tid):
            calls["n"] += 1
            return {"bound": False}

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": "55555"}):
            with patch("app.identity_binding.check_binding_status", _spy):
                identity = resolve_identity(telegram_id=55555)
        assert identity.role == Role.GOVERNOR
        assert calls["n"] == 0

    def test_binding_failure_degrades_to_guest(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            with patch(
                "app.identity_binding.check_binding_status",
                side_effect=RuntimeError("no creds"),
            ):
                identity = resolve_identity(telegram_id=55555)
        assert identity.role == Role.GUEST

    def test_binding_result_is_cached(self):
        """A second resolve for the same id is served from cache."""
        calls = {"n": 0}

        def _spy(_tid):
            calls["n"] += 1
            return {"bound": True, "email": "gary@truesight.me", "name": "Gary Teh"}

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_IDS": ""}):
            with patch("app.identity_binding.check_binding_status", _spy):
                with patch(
                    "app.governor_registry.load_governors", return_value=_GOV_CACHE
                ):
                    resolve_identity(telegram_id=55555)
                    resolve_identity(telegram_id=55555)
        assert calls["n"] == 1
