"""Regression: [TREE PLANTING LINK EVENT] must not be blocked by the
MINTED-only QR duplicate guard in submit_contribution.

Incident 2026-09-23 (thread 35189): a tree-planting link for a SOLD QR was
rejected client-side with "QR code ... has status 'SOLD' - already processed",
but SOLD is the *precondition* for a link (the GAS handler rejects anything
else). The MINTED-only rule is correct for SALES / INVENTORY but wrong for
link events, whose precondition is a later status.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
    from app.config import settings
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


class _FakeEdgar:
    """Stand-in so the execute path makes no network call."""

    def submit_contribution(self, event_name, attributes, description=""):
        return True

    def register_qr_code(self, attributes):
        return True


def _wire(monkeypatch, qr_status):
    monkeypatch.setattr(settings, "require_submission_approval", False)
    monkeypatch.setattr(m, "EdgarDirectClient", _FakeEdgar)
    monkeypatch.setattr(m, "_normalize_submission_labels", lambda e, a: a)
    monkeypatch.setattr(m, "_validate_required_fields", lambda e, a: [])
    monkeypatch.setattr(
        m,
        "lookup_qr_code",
        lambda qr: {
            "status": "success",
            "qr_code": qr,
            "qr_status": qr_status,
            "manager_name": "Kirsten Ritschel",
            "email": "buyer@example.com",
        },
    )
    monkeypatch.setattr(
        m,
        "_resolve_identity",
        lambda display_name=None, **k: SimpleNamespace(
            telegram_id=1, role=SimpleNamespace(value="governor"), name=None
        ),
    )
    monkeypatch.setattr(
        m,
        "_policy_evaluate",
        lambda identity, func_name: SimpleNamespace(allowed=True, reason=""),
    )


def _run(args):
    return asyncio.run(
        m._run_tool(
            "submit_contribution",
            dict(args),
            history=[],
            session_id="tg:1:2",
            governor_name="Gary Teh",
        )
    )


LINK_ARGS = {
    "event_name": "TREE PLANTING LINK EVENT",
    "attributes": {
        "QR Code": "2024OSCAR_CB_20260620_1",
        "SunMint Submission Message ID": "Edgar_20260903083523_003",
        "Updated by": "Sophia Truesight",
    },
}


def test_link_event_with_sold_qr_reaches_execute(monkeypatch):
    """SOLD is the link precondition -> must NOT be treated as a duplicate."""
    _wire(monkeypatch, "SOLD")
    result = _run(LINK_ARGS)
    low = result.lower()
    assert "submitted successfully" in low, result
    assert "duplicate" not in low, result


def test_sales_event_with_sold_qr_still_blocked(monkeypatch):
    """The original guard must still fire for SALES (SOLD = already sold)."""
    _wire(monkeypatch, "SOLD")
    result = _run(
        {
            "event_name": "SALES EVENT",
            "attributes": {
                "Item": "2024OSCAR_CB_20260620_1",
                "QR Code": "2024OSCAR_CB_20260620_1",
                "Sales price": "20",
                "Owner email": "buyer@example.com",
            },
        }
    )
    assert "duplicate" in result.lower(), result
