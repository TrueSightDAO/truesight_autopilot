"""Tests for _refresh_events_catalog merge logic.

Verifies that the live Edgar catalog always wins over the hardcoded
fallback dicts (_CANONICAL_LABELS and _VALIDATE_REQUIRED_FIELDS).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import (
    _CANONICAL_LABELS,
    _CATALOG_URL,
    _VALIDATE_REQUIRED_FIELDS,
    _catalog_last_refresh,
    _refresh_events_catalog,
)


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset the in-memory dicts and refresh timestamp before each test."""
    # Snapshot the original hardcoded values so we can restore them
    orig_labels = dict(_CANONICAL_LABELS)
    orig_required = dict(_VALIDATE_REQUIRED_FIELDS)
    orig_ts = _catalog_last_refresh
    yield
    # Restore after test
    _CANONICAL_LABELS.clear()
    _CANONICAL_LABELS.update(orig_labels)
    _VALIDATE_REQUIRED_FIELDS.clear()
    _VALIDATE_REQUIRED_FIELDS.update(orig_required)
    globals()["_catalog_last_refresh"] = orig_ts


def _mock_response(status=200, data=None):
    """Build a mock httpx response."""
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status
    resp.json = AsyncMock(return_value=data or {"events": {}, "version": "test"})
    resp.raise_for_status = AsyncMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=AsyncMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
async def test_catalog_changed_labels_adopted():
    """Catalog with changed labels for an existing event → catalog wins."""
    # Start with the hardcoded SALES EVENT labels
    original = list(_CANONICAL_LABELS.get("SALES EVENT", []))
    assert "Item" in original

    # Catalog returns SALES EVENT with different labels
    catalog_labels = ["Item", "Sales price", "Sold by", "New Field Added"]
    catalog = {
        "events": {
            "SALES EVENT": {
                "canonical_labels": catalog_labels,
                "required_fields": ["Item", "Sales price"],
            }
        },
        "version": "2.0",
    }

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    assert _CANONICAL_LABELS["SALES EVENT"] == catalog_labels


@pytest.mark.asyncio
async def test_catalog_changed_required_fields_adopted():
    """Catalog with changed required fields for an existing event → adopted."""
    # Start with hardcoded required fields for SALES EVENT
    original = list(_VALIDATE_REQUIRED_FIELDS.get("SALES EVENT", []))
    assert "Item" in original

    # Catalog returns SALES EVENT with different required fields
    catalog_required = ["Item", "Sold by"]  # removed "Sales price"
    catalog = {
        "events": {
            "SALES EVENT": {
                "canonical_labels": ["Item", "Sales price", "Sold by"],
                "required_fields": catalog_required,
            }
        },
        "version": "2.0",
    }

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    assert _VALIDATE_REQUIRED_FIELDS["SALES EVENT"] == catalog_required


@pytest.mark.asyncio
async def test_new_event_added():
    """New event from catalog → still added."""
    assert "NEW TEST EVENT" not in _CANONICAL_LABELS
    assert "NEW TEST EVENT" not in _VALIDATE_REQUIRED_FIELDS

    catalog = {
        "events": {
            "NEW TEST EVENT": {
                "canonical_labels": ["Field A", "Field B"],
                "required_fields": ["Field A"],
            }
        },
        "version": "2.0",
    }

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    assert _CANONICAL_LABELS["NEW TEST EVENT"] == ["Field A", "Field B"]
    assert _VALIDATE_REQUIRED_FIELDS["NEW TEST EVENT"] == ["Field A"]


@pytest.mark.asyncio
async def test_catalog_fewer_labels_than_hardcoded():
    """Catalog with fewer labels than hardcoded → catalog still wins."""
    # INVENTORY MOVEMENT has many hardcoded labels
    original_count = len(_CANONICAL_LABELS.get("INVENTORY MOVEMENT", []))
    assert original_count > 2

    # Catalog returns only 2 labels
    catalog_labels = ["Manager Name", "QR Code"]
    catalog = {
        "events": {
            "INVENTORY MOVEMENT": {
                "canonical_labels": catalog_labels,
                "required_fields": ["Manager Name", "QR Code"],
            }
        },
        "version": "2.0",
    }

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    # Catalog wins even though it has fewer labels
    assert _CANONICAL_LABELS["INVENTORY MOVEMENT"] == catalog_labels


@pytest.mark.asyncio
async def test_empty_catalog_no_crash():
    """Empty catalog → no crash, no change."""
    labels_before = dict(_CANONICAL_LABELS)
    required_before = dict(_VALIDATE_REQUIRED_FIELDS)

    catalog = {"events": {}, "version": "test"}

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    assert _CANONICAL_LABELS == labels_before
    assert _VALIDATE_REQUIRED_FIELDS == required_before


@pytest.mark.asyncio
async def test_catalog_http_error_no_change():
    """HTTP error fetching catalog → no crash, no change."""
    labels_before = dict(_CANONICAL_LABELS)
    required_before = dict(_VALIDATE_REQUIRED_FIELDS)

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(status=500)
        )
        await _refresh_events_catalog()

    assert _CANONICAL_LABELS == labels_before
    assert _VALIDATE_REQUIRED_FIELDS == required_before


@pytest.mark.asyncio
async def test_catalog_updates_both_dicts():
    """Catalog event with both labels and required fields updates both dicts."""
    catalog = {
        "events": {
            "CONTRIBUTION EVENT": {
                "canonical_labels": ["Type", "Amount", "New Label"],
                "required_fields": ["Type", "New Required Field"],
            }
        },
        "version": "3.0",
    }

    with patch("app.main.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_mock_response(data=catalog)
        )
        await _refresh_events_catalog()

    assert _CANONICAL_LABELS["CONTRIBUTION EVENT"] == [
        "Type",
        "Amount",
        "New Label",
    ]
    assert _VALIDATE_REQUIRED_FIELDS["CONTRIBUTION EVENT"] == [
        "Type",
        "New Required Field",
    ]
