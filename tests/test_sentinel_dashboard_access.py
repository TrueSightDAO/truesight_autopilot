"""Sentinel-access tests for the MAP dashboard (scoped, no wider auth widening)."""

import inspect
from unittest.mock import patch

from app.auth import verify_payload


from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def test_verify_payload_has_allow_sentinel_default_false():
    sig = inspect.signature(verify_payload)
    assert sig.parameters["allow_sentinel"].default is False


def test_gate_accepts_sentinel_when_allowed():
    payload = {"timestamp": _now_iso(), "nonce": "n1"}
    sig = "x"
    key = "k"
    with (
        patch("app.auth.verify_rsa_signature", return_value=True),
        patch("app.auth.is_governor", return_value=False),
        patch("app.auth._is_sentinel_from_registry", return_value=True),
    ):
        # should NOT raise 403 when allow_sentinel=True
        verify_payload(payload, sig, key, allow_sentinel=True)


def test_gate_rejects_sentinel_when_not_allowed():
    payload = {"timestamp": _now_iso(), "nonce": "n2"}
    sig = "x"
    key = "k"
    with (
        patch("app.auth.verify_rsa_signature", return_value=True),
        patch("app.auth.is_governor", return_value=False),
        patch("app.auth._is_sentinel_from_registry", return_value=True),
    ):
        try:
            verify_payload(payload, sig, key)  # allow_sentinel defaults False
        except Exception as e:
            assert e.status_code == 403
        else:
            raise AssertionError("expected 403 for sentinel without allow_sentinel")


def test_gate_accepts_governor_even_without_sentinel_flag():
    payload = {"timestamp": _now_iso(), "nonce": "n3"}
    sig = "x"
    key = "k"
    with (
        patch("app.auth.verify_rsa_signature", return_value=True),
        patch("app.auth.is_governor", return_value=True),
    ):
        verify_payload(payload, sig, key)  # default path unchanged
