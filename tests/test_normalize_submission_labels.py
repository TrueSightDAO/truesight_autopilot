"""Regression: a double-encoded `attributes` tool arg must not crash the tool loop.

2026-06-14 Kopi Bay onboarding incident — DeepSeek emitted `submit_contribution`
with `attributes` as a JSON *string* instead of a nested object:

    {"event_name": "CONTRIBUTOR ADD EVENT",
     "attributes": "{\\"Contributor Name\\": \\"Nora - Kopi Bar & Bakery\\", ...}"}

`_normalize_submission_labels` is type-hinted `dict` and did `attributes.items()`,
so a str raised `AttributeError: 'str' object has no attribute 'items'` mid
tool-loop. The turn died before the `tool` result was written, leaving an orphan
`tool_calls` that bricked the thread on every subsequent request.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.main import unavailable in this env: {exc}", allow_module_level=True
    )


def test_stringified_attributes_does_not_crash():
    """A JSON-string `attributes` is parsed, not crashed on."""
    stringified = (
        '{"Contributor Name": "Nora - Kopi Bar & Bakery", '
        '"Contributor Email": "nora@noraharon.com"}'
    )
    # Previously raised AttributeError: 'str' object has no attribute 'items'
    out = m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", stringified)
    assert isinstance(out, dict)
    # The parsed keys are preserved through normalization.
    assert "Contributor Name" in out
    assert out["Contributor Name"] == "Nora - Kopi Bar & Bakery"


def test_dict_attributes_still_work():
    """The normal object form is unchanged."""
    out = m._normalize_submission_labels(
        "CONTRIBUTOR ADD EVENT",
        {"Contributor Name": "Nora", "Contributor Email": "nora@noraharon.com"},
    )
    assert out["Contributor Name"] == "Nora"


def test_garbage_attributes_degrade_gracefully():
    """A non-JSON string or wrong type yields an empty dict, never an exception."""
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", "not json {{{") == {}
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", None) == {}
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", 123) == {}


# ── Catalog-driven normalizer tests (PR4) ────────────────────────────────


def _set_catalog_normalize(val: bool):
    """Helper to toggle the flag for a test block."""
    import app.config as cfg

    old = cfg.settings.catalog_normalize
    cfg.settings.catalog_normalize = val
    return old


def test_normalize_via_catalog_exact_match():
    """Exact match → canonical label used as-is."""
    labels = ["Manager Name", "QR Code", "Quantity"]
    attrs = {"Manager Name": "Alice", "QR Code": "QR001", "Quantity": "5"}
    out = m._normalize_via_catalog(attrs, labels)
    assert out == {"Manager Name": "Alice", "QR Code": "QR001", "Quantity": "5"}


def test_normalize_via_catalog_case_insensitive():
    """Case-insensitive match → canonical label used."""
    labels = ["Manager Name", "QR Code", "Quantity"]
    attrs = {"manager name": "Alice", "qr code": "QR001", "quantity": "5"}
    out = m._normalize_via_catalog(attrs, labels)
    assert out == {"Manager Name": "Alice", "QR Code": "QR001", "Quantity": "5"}


def test_normalize_via_catalog_space_underscore_hyphen():
    """Space/underscore/hyphen normalized match → canonical label used."""
    labels = ["Manager Name", "QR Code", "Sales price"]
    attrs = {"Manager_Name": "Alice", "qr-code": "QR001", "sales_price": "10.00"}
    out = m._normalize_via_catalog(attrs, labels)
    assert out == {"Manager Name": "Alice", "QR Code": "QR001", "Sales price": "10.00"}


def test_normalize_via_catalog_alias_fallback():
    """No catalog match → _FIELD_ALIASES fallback."""
    labels = ["Manager Name", "QR Code"]
    attrs = {"manager": "Alice", "qr": "QR001"}
    out = m._normalize_via_catalog(attrs, labels)
    assert out == {"Manager Name": "Alice", "QR Code": "QR001"}


def test_normalize_via_catalog_unmatched_keys_kept():
    """Keys that don't match any canonical label are kept (not silently dropped)."""
    labels = ["Manager Name", "QR Code"]
    attrs = {"Manager Name": "Alice", "Extra Field": "some value"}
    out = m._normalize_via_catalog(attrs, labels)
    assert out == {"Manager Name": "Alice", "Extra Field": "some value"}


def test_normalize_via_catalog_empty_labels():
    """Empty canonical_labels list → all keys pass through unchanged."""
    attrs = {"Some Key": "value", "Another": "val"}
    out = m._normalize_via_catalog(attrs, [])
    assert out == attrs


def test_normalize_via_catalog_empty_attrs():
    """Empty attributes dict → empty result."""
    out = m._normalize_via_catalog({}, ["Manager Name", "QR Code"])
    assert out == {}


def test_catalog_normalize_flag_on_inventory_movement():
    """CATALOG_NORMALIZE=True: INVENTORY MOVEMENT with various key forms."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "INVENTORY MOVEMENT",
            {
                "manager_name": "Alice",
                "recipient": "Bob",
                "inventory_item": "Cacao",
                "qr_code": "QR001",
                "qty": "10",
                "latitude": "-8.5",
                "longitude": "115.2",
            },
        )
        assert out.get("Manager Name") == "Alice"
        assert out.get("Recipient Name") == "Bob"
        assert out.get("Inventory Item") == "Cacao"
        assert out.get("QR Code") == "QR001"
        assert out.get("Quantity") == "10"
        assert out.get("Latitude") == "-8.5"
        assert out.get("Longitude") == "115.2"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_sales_event():
    """CATALOG_NORMALIZE=True: SALES EVENT with various key forms."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "SALES EVENT",
            {
                "item": "Cacao Pouch",
                "sales_price": "25.00",
                "sold_by": "Alice",
                "cash_proceeds_collected_by": "Bob",
                "owner_email": "alice@example.com",
                "stripe_session_id": "cs_test_123",
                "shipping_provider": "UPS",
                "tracking_number": "1Z999AA10123456784",
            },
        )
        assert out.get("Item") == "Cacao Pouch"
        assert out.get("Sales price") == "25.00"
        assert out.get("Sold by") == "Alice"
        assert out.get("Cash proceeds collected by") == "Bob"
        assert out.get("Owner email") == "alice@example.com"
        assert out.get("Stripe Session ID") == "cs_test_123"
        assert out.get("Shipping Provider") == "UPS"
        assert out.get("Tracking number") == "1Z999AA10123456784"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_contribution_event():
    """CATALOG_NORMALIZE=True: CONTRIBUTION EVENT."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "CONTRIBUTION EVENT",
            {
                "type": "Time (Minutes)",
                "amount": "120",
                "description": "Worked on documentation",
                "contributors": "Alice, Bob",
                "tdg_issued": "50",
            },
        )
        assert out.get("Type") == "Time (Minutes)"
        assert out.get("Amount") == "120"
        assert out.get("Description") == "Worked on documentation"
        assert out.get("Contributor(s)") == "Alice, Bob"
        assert out.get("TDG Issued") == "50"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_qr_code_registration():
    """CATALOG_NORMALIZE=True: QR CODE REGISTRATION."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "QR CODE REGISTRATION",
            {
                "qr_code": "QR001",
                "landing_page": "https://example.com",
                "farm_name": "Kopi Farm",
                "state": "Bali",
                "country": "Indonesia",
                "year": "2026",
                "currency": "USD",
                "status": "ACTIVE",
                "manager": "Alice",
                "creation_date": "2026-01-15",
            },
        )
        assert out.get("QR Code") == "QR001"
        assert out.get("Landing Page") == "https://example.com"
        assert out.get("Farm Name") == "Kopi Farm"
        assert out.get("State") == "Bali"
        assert out.get("Country") == "Indonesia"
        assert out.get("Year") == "2026"
        assert out.get("Currency") == "USD"
        assert out.get("Status") == "ACTIVE"
        assert out.get("Manager") == "Alice"
        assert out.get("Creation Date") == "2026-01-15"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_tree_planting_event():
    """CATALOG_NORMALIZE=True: TREE PLANTING EVENT (no old alias map)."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "TREE PLANTING EVENT",
            {
                "number_of_trees_planted": "50",
                "species": "Mahogany",
                "location": "Bali",
                "attached_filename": "photo.jpg",
                "submission_source": "autopilot",
            },
        )
        assert out.get("Number of trees planted") == "50"
        assert out.get("Species") == "Mahogany"
        assert out.get("Location") == "Bali"
        assert out.get("Attached Filename") == "photo.jpg"
        assert out.get("Submission Source") == "autopilot"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_dao_inventory_expense_event():
    """CATALOG_NORMALIZE=True: DAO Inventory Expense Event (no old alias map)."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "DAO Inventory Expense Event",
            {
                "dao_member_name": "Alice",
                "target_ledger": "Main Ledger",
                "latitude": "-8.5",
                "longitude": "115.2",
                "inventory_type": "Seeds",
                "inventory_quantity": "100",
                "description": "Seeds for planting",
                "attached_filename": "receipt.pdf",
                "destination_inventory_file_location": "Farm A",
            },
        )
        assert out.get("DAO Member Name") == "Alice"
        assert out.get("Target Ledger") == "Main Ledger"
        assert out.get("Latitude") == "-8.5"
        assert out.get("Longitude") == "115.2"
        assert out.get("Inventory Type") == "Seeds"
        assert out.get("Inventory Quantity") == "100"
        assert out.get("Description") == "Seeds for planting"
        assert out.get("Attached Filename") == "receipt.pdf"
        assert out.get("Destination Inventory File Location") == "Farm A"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_off_unchanged():
    """CATALOG_NORMALIZE=False (default): legacy behavior unchanged."""
    old = _set_catalog_normalize(False)
    try:
        out = m._normalize_submission_labels(
            "INVENTORY MOVEMENT",
            {"manager_name": "Alice", "recipient": "Bob", "item": "Cacao"},
        )
        assert out.get("Manager Name") == "Alice"
        assert out.get("Recipient Name") == "Bob"
        assert out.get("Inventory Item") == "Cacao"
    finally:
        _set_catalog_normalize(old)


def test_catalog_normalize_flag_on_keeps_unmatched_keys():
    """CATALOG_NORMALIZE=True: keys not in canonical_labels are kept."""
    old = _set_catalog_normalize(True)
    try:
        out = m._normalize_submission_labels(
            "INVENTORY MOVEMENT",
            {"Manager Name": "Alice", "Unknown Field": "some value"},
        )
        assert out.get("Manager Name") == "Alice"
        assert out.get("Unknown Field") == "some value"
    finally:
        _set_catalog_normalize(old)
