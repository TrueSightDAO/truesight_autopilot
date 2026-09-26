"""Brain-side gate tests for brain tier-awareness PR2.

The adapter asserts a turn's tier on the SIGNED ``author_role`` JWT claim
(PR1, the transport half). This file pins the *brain* half: a turn whose
asserted role is not ``governor`` must NOT be able to run a state-changing
tool, even though the request rides the governor's public key.

Why this matters (the vulnerability): the adapters mint the JWT for the
*govenor's* public key, so ``_gov_name_for_key(public_key)`` is truthy on a
member's turn and the pre-existing WRITE gate passed on ``governor_name``
alone -- a member turn would inherit governor write power (privilege
escalation). The gate now requires the signed claim to say ``governor``.

Back-compat rule pinned by PR1: an ABSENT/unknown claim resolves to
``governor``, so pre-existing governor traffic is unchanged.
"""

from __future__ import annotations

import json

import pytest

from app import main as app_main
from app.auth import DEFAULT_AUTHOR_ROLE, author_role_from_claims

# A representative WRITE tool (state-changing, governor-only) and a READ tool.
WRITE_TOOL = "merge_pr"
READ_TOOL = "lookup_qr_code"


def test_default_author_role_is_governor():
    # The back-compat anchor: absent claim must mean governor, or every legacy
    # caller silently loses write access.
    assert DEFAULT_AUTHOR_ROLE == "governor"
    assert author_role_from_claims({}) == "governor"
    assert author_role_from_claims({"sub": "K"}) == "governor"


@pytest.mark.parametrize("role", ["member", "guest"])
def test_write_tool_blocked_for_non_governor(role):
    """A member/guest turn cannot run a state-changing tool.

    ``governor_name`` is deliberately set (the public key resolves to a real
    governor) to prove the claim -- not the key -- is what gates.
    """
    out = app_main._run_tool_sync(
        WRITE_TOOL,
        {"repo": "truesight_autopilot", "pr_number": 1},
        governor_name="Gary Teh",
        session_id="test",
        author_role=role,
    )
    payload = json.loads(out)
    assert payload["status"] == "blocked"
    # The reason must name the actual tier, so the governor can tell an
    # escalation attempt from a missing-identity block.
    assert role in payload["message"]
    assert "governor" in payload["message"].lower()


def test_write_tool_blocked_for_non_governor_without_governor_name():
    """Even with no governor_name, a non-governor role stays blocked."""
    out = app_main._run_tool_sync(
        WRITE_TOOL, {}, governor_name=None, session_id="test", author_role="member"
    )
    assert json.loads(out)["status"] == "blocked"


def test_governor_role_passes_tier_check(monkeypatch):
    """author_role='governor' clears the tier gate and reaches dispatch.

    We stub the registry dispatch so no real tool body runs -- we only assert
    the call was NOT short-circuited into a block.
    """
    sentinel = json.dumps({"status": "ok", "stub": True})

    def _fake_dispatch(func_name, func_args, ctx):
        return sentinel

    import app.tool_registry as tool_registry

    monkeypatch.setattr(tool_registry, "dispatch", _fake_dispatch)

    out = app_main._run_tool_sync(
        WRITE_TOOL,
        {"repo": "truesight_autopilot", "pr_number": 1},
        governor_name="Gary Teh",
        session_id="test",
        author_role="governor",
    )
    assert out == sentinel


def test_read_tool_not_gated_by_author_role():
    """READ tools stay available to members (ask / research / draft).

    A member must be able to look things up -- only WRITE/ADMIN is gated.
    We assert a READ tool is not rejected by the *tier* gate (it may of
    course fail later for unrelated reasons in a bare test environment).
    """
    out = app_main._run_tool_sync(
        READ_TOOL,
        {"qr_code": "2024OSCAR_20260121_12"},
        governor_name=None,
        session_id="test",
        author_role="member",
    )
    # The tier gate's exact block message must NOT be what came back.
    assert "authenticated as 'member'" not in out


def test_missing_author_role_defaults_to_governor(monkeypatch):
    """Omitting author_role keeps legacy governor behaviour (regression guard)."""
    sentinel = json.dumps({"status": "ok", "stub": True})

    import app.tool_registry as tool_registry

    monkeypatch.setattr(tool_registry, "dispatch", lambda *a, **k: sentinel)

    out = app_main._run_tool_sync(
        WRITE_TOOL,
        {"repo": "truesight_autopilot", "pr_number": 1},
        governor_name="Gary Teh",
        session_id="test",
    )
    assert out == sentinel
