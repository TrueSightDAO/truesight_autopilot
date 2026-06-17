"""lookup_event_docs tool — fetch DAO event documentation from Edgar landing page.

Returns the format, required fields, and when-to-use rules for a given event type.
Docs are fetched live from the Edgar landing page so they're always current.
"""

import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EDGAR_LANDING_URL = "https://edgar.truesight.me/"

# Fallback docs in case Edgar is unreachable — mirrors the landing page content
FALLBACK_DOCS = {
    "SALES EVENT": {
        "when_to_use": "Use when a bag is sold to an end customer (retail sale). Money changes hands. QR status will be updated to SOLD.",
        "required_fields": ["Item (QR code)", "Sales price"],
        "optional_fields": ["Owner email", "Stripe Session ID", "Shipping Provider", "Tracking number", "Sold by", "Cash proceeds collected by"],
        "example": "[SALES EVENT]\n- Item: 2024OSCAR_20260121_32\n- Sales price: 17.50\n- Sold by: Gergana - The Way Home Shop\n- Cash proceeds collected by: Gary Teh"
    },
    "INVENTORY MOVEMENT": {
        "when_to_use": "Use when a bag moves between known holders in the supply chain (e.g. from warehouse to retailer). NOT for end-customer sales.",
        "required_fields": ["Manager Name (sender)", "Recipient Name", "Inventory Item (SKU)", "QR Code", "Quantity"],
        "optional_fields": [],
        "example": "[INVENTORY MOVEMENT]\n- Manager Name: Kirsten\n- Recipient Name: Gergana - The Way Home Shop\n- Inventory Item: Ceremonial Cacao Kraft Pouch - Oscar 2024\n- QR Code: 2024OSCAR_20260121_32\n- Quantity: 1"
    },
    "CONTRIBUTION EVENT": {
        "when_to_use": "Use to record a contributor's time, work, or value-add to the DAO. Earns TDG tokens.",
        "required_fields": ["Contributor", "Description of work", "Amount (minutes or USD)"],
        "optional_fields": ["PR URLs", "TDG issued"],
        "example": "[CONTRIBUTION EVENT]\n- Contributor: Sophia Truesight\n- Description: Built query endpoints for Edgar\n- Amount: 120 minutes\n- PR URLs: https://github.com/TrueSightDAO/dao_protocol/pull/116"
    }
}


def _parse_event_docs_from_html(html: str, event_name: str) -> dict[str, Any] | None:
    """Parse the DAO Events Reference section from the Edgar landing page HTML."""
    # Find the DAO Events Reference section
    section_match = re.search(
        r'<h2[^>]*>DAO Events Reference</h2>(.*?)(?=<h2|$)',
        html, re.DOTALL | re.IGNORECASE
    )
    if not section_match:
        return None

    section = section_match.group(1)

    # Find the specific event card
    # Each event is in a <details> or <div> with the event name
    event_pattern = re.compile(
        rf'<details[^>]*>.*?{re.escape(event_name)}.*?(?=</details>)',
        re.DOTALL | re.IGNORECASE
    )
    event_match = event_pattern.search(section)
    if not event_match:
        # Try simpler pattern — look for the event name as a heading
        heading_pattern = re.compile(
            rf'<h3[^>]*>.*?{re.escape(event_name)}.*?</h3>(.*?)(?=<h3|$)',
            re.DOTALL | re.IGNORECASE
        )
        heading_match = heading_pattern.search(section)
        if not heading_match:
            return None
        event_html = heading_match.group(0)
    else:
        event_html = event_match.group(0)

    # Extract text content, stripping HTML tags
    text = re.sub(r'<[^>]+>', ' ', event_html)
    text = re.sub(r'\s+', ' ', text).strip()

    return {
        "event_name": event_name,
        "raw_text": text,
        "note": "Parsed from Edgar landing page. Verify against live docs for complete formatting."
    }


def _get_fallback_doc(event_name: str) -> dict[str, Any] | None:
    """Return fallback docs for known event types."""
    for key, doc in FALLBACK_DOCS.items():
        if event_name.upper() == key or event_name.upper() in key:
            return {
                "event_name": key,
                "source": "fallback (Edgar unreachable)",
                **doc
            }
    return None


def lookup_event_docs(event_name: str) -> dict[str, Any]:
    """
    Fetch DAO event documentation for the given event type.

    Fetches from the Edgar landing page first. Falls back to built-in docs
    if Edgar is unreachable.

    Args:
        event_name: The event type to look up (e.g. "SALES EVENT", "INVENTORY MOVEMENT")

    Returns:
        Dict with event_name, when_to_use, required_fields, optional_fields, example
    """
    try:
        resp = httpx.get(EDGAR_LANDING_URL, timeout=10)
        resp.raise_for_status()
        html = resp.text

        parsed = _parse_event_docs_from_html(html, event_name)
        if parsed:
            logger.info(f"lookup_event_docs: found docs for {event_name} on Edgar landing page")
            return parsed

        logger.warning(f"lookup_event_docs: event {event_name} not found on Edgar landing page, trying fallback")
    except Exception as e:
        logger.warning(f"lookup_event_docs: failed to fetch from Edgar: {e}, using fallback")

    # Fallback
    fallback = _get_fallback_doc(event_name)
    if fallback:
        return fallback

    return {
        "event_name": event_name,
        "error": f"Event type '{event_name}' not found in documentation.",
        "available_events": list(FALLBACK_DOCS.keys()),
        "note": "Check the Edgar landing page (https://edgar.truesight.me/) for the full DAO Events Reference."
    }
