"""Payout must be resolvable even when Edgar is unreachable.

The `[PAYOUT EVENT]` was added to Edgar's live catalog (`events_catalog.json` v8),
but the autopilot's built-in fallbacks are what an LLM sees when Edgar is
unreachable (or before a catalog deploy lands). If the fallbacks are payout-blind,
Sophia -- and any other LLM -- is BLOCKED from filing a payout. This pins the
fallback surface so a future refactor cannot silently drop it again.

Scope note: this is the DISBURSEMENT receipt (`[PAYOUT EVENT]`), which carries no
raw recipient PII. It is a DIFFERENT event from `[PAYOUT REGISTRATION]` (the P4
intake form, which does carry a raw PIX).
"""

from __future__ import annotations

import pytest

from app.tools import lookup_event_docs as led


@pytest.fixture(autouse=True)
def _force_fallback(monkeypatch):
    """Simulate Edgar unreachable so the built-in fallbacks are exercised."""
    monkeypatch.setattr(led, "_catalog", {}, raising=False)


def test_payout_event_present_in_all_three_fallback_surfaces():
    assert "PAYOUT EVENT" in led._FALLBACK_DOCS
    assert "PAYOUT EVENT" in led._IMPORTANT_FIELDS
    assert "PAYOUT EVENT" in led._INTENT_GUIDANCE.values()


def test_direct_lookup_returns_the_catalog_contract_from_fallback():
    r = led.lookup_event_docs("PAYOUT EVENT")
    assert r["event_name"] == "PAYOUT EVENT"
    assert r["dapp_page"] == "report_payout_event.html"
    # the six required fields mirror the live catalog entry (v8)
    assert r["required_fields"] == [
        "Program",
        "Amount",
        "Currency",
        "Paid At",
        "Bank Ref",
        "Recipient",
    ]


@pytest.mark.parametrize(
    "intent",
    ["payout", "pay the planter", "record payout", "pix payout", "disburse"],
)
def test_common_intents_resolve_to_payout_event(intent):
    r = led.lookup_event_docs(intent)
    assert r["event_name"] == "PAYOUT EVENT", f"{intent!r} did not resolve"


def test_payout_fallback_forbids_raw_pii_by_absence_and_wording():
    """SS12.1 -- the payout carries no raw PIX/account, only a pk_hash."""
    desc = led._FALLBACK_DOCS["PAYOUT EVENT"]["description"]
    assert "PK Hash" in desc
    assert "no raw recipient pii" in desc.lower()
    # the surface must not invite a raw pix field
    joined = " ".join(led._IMPORTANT_FIELDS["PAYOUT EVENT"]).lower()
    assert "pix" not in joined


def test_payout_is_distinct_from_payout_registration():
    """Do not conflate the disbursement receipt with the P4 intake form."""
    desc = led._FALLBACK_DOCS["PAYOUT EVENT"]["description"]
    assert "PAYOUT REGISTRATION" in desc
    assert "PAYMENT EVENT" in desc


def test_validation_gate_knows_payout_event():
    """`app.main`'s client-side required-field gate must not block a payout."""
    from app.main import _CANONICAL_LABELS, _VALIDATE_REQUIRED_FIELDS

    assert _VALIDATE_REQUIRED_FIELDS["PAYOUT EVENT"] == [
        "Program",
        "Amount",
        "Currency",
        "Paid At",
        "Bank Ref",
        "Recipient",
    ]
    assert "PAYOUT EVENT" in _CANONICAL_LABELS
