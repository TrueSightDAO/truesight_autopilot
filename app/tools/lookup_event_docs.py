"""lookup_event_docs tool — fetch DAO event documentation from Edgar events catalog.

Returns the canonical labels, required fields, category, description, intent-to-event
guidance, and important-fields hints for a given event type. Fetches live from
edgar.truesight.me/dao-protocol/events-catalog (JSON) so it's always current.
Falls back to built-in docs if Edgar is unreachable.

This is the single source of truth — no hardcoded event definitions.
"""

import json
import logging
from typing import Any

import httpx

from ..tool_registry import ToolSpec

logger = logging.getLogger(__name__)

# Primary: live JSON catalog from Edgar
CATALOG_URL = "https://edgar.truesight.me/events-catalog"

# Cached catalog (refreshed on miss)
_catalog: dict[str, Any] | None = None

# ── Intent-to-event mapping ───────────────────────────────────────────────
# Maps common governor intents to the correct DAO event type. The LLM should
# consult this BEFORE calling submit_contribution so it picks the right event.
_INTENT_GUIDANCE: dict[str, str] = {
    "sell cacao": "SALES EVENT",
    "sale": "SALES EVENT",
    "retail sale": "SALES EVENT",
    "end customer sale": "SALES EVENT",
    "transfer custody": "INVENTORY MOVEMENT",
    "transfer bag": "INVENTORY MOVEMENT",
    "move inventory": "INVENTORY MOVEMENT",
    "supply chain transfer": "INVENTORY MOVEMENT",
    "record work": "CONTRIBUTION EVENT",
    "log contribution": "CONTRIBUTION EVENT",
    "record time": "CONTRIBUTION EVENT",
    "add partner": "PARTNER ADD EVENT",
    "onboard partner": "PARTNER ADD EVENT",
    "check in with partner": "PARTNER CHECK-IN EVENT",
    "partner check-in": "PARTNER CHECK-IN EVENT",
    "register qr code": "QR CODE REGISTRATION",
    "add contributor": "CONTRIBUTOR ADD EVENT",
    "onboard contributor": "CONTRIBUTOR ADD EVENT",
    "capital injection": "CAPITAL INJECTION EVENT",
    "record payment": "PAYMENT EVENT",
}

# ── Important fields per event type ───────────────────────────────────────
# Fields that are most commonly missed or incorrectly filled by the LLM.
# The LLM should ensure these are always present when submitting.
_IMPORTANT_FIELDS: dict[str, list[str]] = {
    "SALES EVENT": [
        "Cash proceeds collected by",
        "Owner email",
        "Sales price",
        "Item",
        "Sold by",
    ],
    "INVENTORY MOVEMENT": [
        "Manager Name",
        "Recipient Name",
        "QR Code",
        "Quantity",
        "Destination inventory file location",
    ],
    "CONTRIBUTION EVENT": [
        "Type",
        "Amount",
        "Contributor",
    ],
    "PARTNER ADD EVENT": [
        "Partner Name",
        "Partner Email",
        "Partner Type",
    ],
    "PARTNER CHECK-IN EVENT": [
        "Partner Name",
        "Check-in Date",
        "Notes",
    ],
    "QR CODE REGISTRATION": [
        "QR Code",
        "Item",
        "Manager",
    ],
    "CONTRIBUTOR ADD EVENT": [
        "Contributor Name",
        "Contributor Email",
        "Role",
    ],
    "CAPITAL INJECTION EVENT": [
        "Amount",
        "Source",
        "Date",
    ],
    "PAYMENT EVENT": [
        "Amount",
        "Paid To",
        "Paid By",
    ],
}

# Minimal fallback for when Edgar is unreachable
_FALLBACK_DOCS: dict[str, dict[str, Any]] = {
    "SALES EVENT": {
        "description": "Use when a bag is sold to an end customer (retail sale). QR status updated to SOLD.",
        "required_fields": ["Item", "Sales price", "Sold by"],
        "dapp_page": "report_sales.html",
    },
    "INVENTORY MOVEMENT": {
        "description": "Use when a bag moves between known holders in the supply chain. NOT for end-customer sales.",
        "required_fields": ["Manager Name", "Recipient Name", "QR Code"],
        "dapp_page": "report_inventory_movement.html",
    },
    "CONTRIBUTION EVENT": {
        "description": "Use to record a contributor's time, work, or value-add to the DAO. Earns TDG.",
        "required_fields": ["Type", "Amount"],
        "dapp_page": "report_contribution.html",
    },
}


def _fetch_catalog() -> dict[str, Any]:
    """Fetch and return the live events catalog, or {} on failure."""
    global _catalog
    try:
        resp = httpx.get(CATALOG_URL, timeout=15)
        resp.raise_for_status()
        _catalog = resp.json()
        logger.info("events catalog loaded: %d events (version=%s)",
                     len(_catalog.get("events", {})), _catalog.get("version"))
        return _catalog
    except Exception as exc:
        logger.warning("Failed to fetch events catalog from %s: %s", CATALOG_URL, exc)
        if _catalog is not None:
            return _catalog
        return {}


def _find_event(catalog: dict, event_name: str) -> dict[str, Any] | None:
    """Look up an event in the catalog, case-insensitive."""
    events = catalog.get("events", {})
    # Direct key match
    if event_name in events:
        return events[event_name]
    # Case-insensitive match
    upper = event_name.upper()
    for key, val in events.items():
        if key.upper() == upper:
            return val
    # Partial match (e.g. "REPACKAGING" matches "REPACKAGING BATCH EVENT")
    for key, val in events.items():
        if upper in key.upper() or key.upper() in upper:
            return val
    return None


def _build_result(event_name: str, entry: dict) -> dict[str, Any]:
    return {
        "event_name": event_name,
        "category": entry.get("category", "Other"),
        "canonical_labels": entry.get("canonical_labels", []),
        "required_fields": entry.get("required_fields", []),
        "description": entry.get("description", ""),
        "dapp_page": entry.get("dapp_page", ""),
        "source": "edgar-catalog (live)",
    }


def lookup_event_docs(event_name: str) -> dict[str, Any]:
    """
    Fetch DAO event documentation for the given event type from Edgar's live catalog.

    Args:
        event_name: The event type to look up (e.g. "SALES EVENT", "INVENTORY MOVEMENT")

    Returns:
        Dict with event_name, category, canonical_labels, required_fields, description, dapp_page
    """
    catalog = _fetch_catalog()
    entry = _find_event(catalog, event_name)

    if entry:
        logger.info("lookup_event_docs: found %s in live catalog", event_name)
        return _build_result(event_name, entry)

    # Try fallback
    upper = event_name.upper()
    for key, doc in _FALLBACK_DOCS.items():
        if upper == key or upper in key:
            logger.info("lookup_event_docs: found %s in fallback docs", event_name)
            return {
                "event_name": key,
                "category": "Other",
                "canonical_labels": [],
                "required_fields": doc.get("required_fields", []),
                "description": doc.get("description", ""),
                "dapp_page": doc.get("dapp_page", ""),
                "source": "fallback (Edgar unreachable)",
            }

    # Completely unknown
    available = list((catalog.get("events") or {}).keys()) or list(_FALLBACK_DOCS.keys())
    return {
        "event_name": event_name,
        "error": f"Event type '{event_name}' not found in documentation.",
        "available_events": available,
        "note": f"Check {CATALOG_URL} for the full DAO events catalog."
    }


def refresh_events_catalog() -> dict[str, Any]:
    """Force-refresh the catalog. Called at startup by the lifespan handler."""
    global _catalog
    _catalog = None
    return _fetch_catalog()


TOOL_SPEC = ToolSpec(
    name="lookup_event_docs",
    description=(
        "Fetch DAO event documentation for a given event type (e.g. SALES EVENT, "
        "INVENTORY MOVEMENT). Returns canonical labels, required fields, category, "
        "and when-to-use rules. Fetches live from Edgar's event catalog; falls back "
        "to built-in docs if Edgar is unreachable. Always call this BEFORE calling "
        "submit_contribution to ensure you use the correct event type and format."
    ),
    parameters={
        "type": "object",
        "properties": {
            "event_name": {
                "type": "string",
                "description": "The event type to look up, e.g. 'SALES EVENT', 'INVENTORY MOVEMENT', 'PARTNER ADD EVENT', 'CONTRIBUTOR ADD EVENT'",
            }
        },
        "required": ["event_name"],
    },
    handler=lambda args, ctx: json.dumps(lookup_event_docs(args.get("event_name", ""))),
)
