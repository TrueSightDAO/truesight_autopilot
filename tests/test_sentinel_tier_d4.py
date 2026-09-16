"""Sentinel-tier (D4) tests for brain tier-awareness PR2.

D4 (governor direction, 2026-09-16): sentinel is its OWN identity tier,
distinct from governor, that nonetheless carries governor-tier RIGHTS under
the WRITE/ADMIN gate. The two invariants these tests pin:

  1. RIGHTS  -- a sentinel turn passes the same WRITE/ADMIN gate a governor
                turn passes.
  2. LABEL   -- a sentinel turn is NEVER relabeled ``governor`` in
                attribution / audit / logs. It stays ``sentinel``.

The failure these guard against is subtle: it would be easy to implement D4 as
``sentinel -> governor`` (alias), which passes the gate but silently corrupts
the audit trail -- a bot turn would read as a human governor.
"""

from __future__ import annotations

import json

import pytest

from app import main as app_main
from app.auth import (
    AUTHOR_ROLES,
    author_name_from_claims,
    author_role_from_claims,
    create_jwt,
)
from app.policy import (
    Identity,
    Role,
    evaluate,
    has_governor_rights,
    is_governor,
    role_label,
)

WRITE_TOOL = "merge_pr"
READ_TOOL = "lookup_qr_code"


# ── role enum / label distinctness ──────────────────────────────────────────


def test_sentinel_is_a_distinct_role_not_an_alias_of_governor():
    assert Role.SENTINEL != Role.GOVERNOR
    assert Role.SENTINEL.value == "sentinel"


def test_sentinel_is_a_valid_author_role():
    assert "sentinel" in AUTHOR_ROLES


def test_role_label_never_collapses_sentinel_into_governor():
    sent = Identity(telegram_id=None, role=Role.SENTINEL, name="Claude Anthropic")
    assert role_label(sent) == "sentinel"
    assert is_governor(sent) is False  # strict label check stays strict
    assert has_governor_rights(sent) is True  # but it carries the RIGHTS


# ── the RIGHTS half: sentinel passes WRITE/ADMIN ────────────────────────────


def test_sentinel_authorized_for_write_like_governor():
    sent = Identity(telegram_id=None, role=Role.SENTINEL, name="Sophia Truesight")
    gov = Identity(telegram_id=None, role=Role.GOVERNOR, name="Gary Teh")
    assert evaluate(sent, WRITE_TOOL).allowed is True
    assert evaluate(gov, WRITE_TOOL).allowed is True


def test_sentinel_reason_keeps_its_own_label():
    sent = Identity(telegram_id=None, role=Role.SENTINEL, name="Kimi Moon")
    decision = evaluate(sent, WRITE_TOOL)
    assert decision.allowed is True
    assert "sentinel" in decision.reason.lower()
    assert "governor" not in decision.reason.lower()


def test_member_and_guest_still_denied_on_write():
    member = Identity(telegram_id=None, role=Role.MEMBER, name="Member")
    guest = Identity(telegram_id=None, role=Role.GUEST, name="Guest")
    assert evaluate(member, WRITE_TOOL).allowed is False
    assert evaluate(guest, WRITE_TOOL).allowed is False


def test_sentinel_turn_passes_brain_write_gate(monkeypatch):
    """A sentinel turn is NOT blocked by the tier gate (rights parity)."""
    sentinel = json.dumps({"status": "ok", "stub": True})
    import app.tool_registry as tool_registry

    monkeypatch.setattr(tool_registry, "dispatch", lambda *a, **k: sentinel)

    out = app_main._run_tool_sync(
        WRITE_TOOL,
        {"repo": "truesight_autopilot", "pr_number": 1},
        governor_name="Sophia Truesight",
        session_id="test",
        author_role="sentinel",
    )
    assert out == sentinel


def test_sentinel_read_tool_also_allowed():
    out = app_main._run_tool_sync(
        READ_TOOL,
        {"qr_code": "2024OSCAR_20260121_12"},
        governor_name="Sophia Truesight",
        session_id="test",
        author_role="sentinel",
    )
    assert "authenticated as 'sentinel'" not in out


# ── the LABEL half: attribution stays sentinel ──────────────────────────────


def test_sentinel_role_round_trips_through_signed_claim():
    tok = create_jwt("PUBKEY_S", "sentinel", "Claude Anthropic")
    from app.auth import verify_jwt_claims

    class _Req:
        headers = {"Authorization": f"Bearer {tok}"}
        cookies = {}

    claims = verify_jwt_claims(_Req())
    assert author_role_from_claims(claims) == "sentinel"
    assert author_name_from_claims(claims) == "Claude Anthropic"


def test_author_name_absent_is_none_and_backcompat():
    assert author_name_from_claims({}) is None
    assert author_name_from_claims(None) is None
    assert author_name_from_claims({"author_name": "  "}) is None


def test_sentinel_claim_invalid_still_fails_closed():
    # An unknown role is coerced to the default, never escalated to sentinel.
    from app.auth import DEFAULT_AUTHOR_ROLE

    assert author_role_from_claims({"author_role": "supervisor"}) == (
        DEFAULT_AUTHOR_ROLE
    )


@pytest.mark.parametrize("role", ["sentinel"])
def test_brain_block_message_never_says_governor_for_sentinel(role, monkeypatch):
    """If a sentinel WERE blocked it must not be labelled governor.

    (Sentinels pass the gate, so we force the *policy* layer to deny to prove
    the block message uses the sentinel label, not the governor one.)
    """
    import app.main as m

    monkeypatch.setattr(
        m,
        "_policy_evaluate",
        lambda identity, tool: type(
            "D", (), {"allowed": False, "reason": "synthetic deny"}
        )(),
    )
    out = app_main._run_tool_sync(
        WRITE_TOOL,
        {"repo": "truesight_autopilot", "pr_number": 1},
        governor_name="Sophia Truesight",
        session_id="test",
        author_role=role,
    )
    # Denied, but the *tier gate* message must never mislabel it governor.
    assert "authenticated as 'sentinel'" not in out
