#!/usr/bin/env python3
"""Fetch the live Edgar events catalog and write it as a committed JSON snapshot.

Usage:
    python scripts/fetch_events_snapshot.py

Writes to ``app/data/events_catalog_snapshot.json``.  This snapshot is used as
the offline fallback when Edgar is unreachable at startup or during a refresh
cycle, so the autopilot always has the full ~30-event catalog available even
without network access to Edgar.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(message)s"
)
logger = logging.getLogger("fetch_events_snapshot")

_CATALOG_URL = "https://edgar.truesight.me/events-catalog"
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "events_catalog_snapshot.json"


def main() -> int:
    logger.info("Fetching events catalog from %s …", _CATALOG_URL)
    try:
        resp = httpx.get(_CATALOG_URL, timeout=30.0)
        resp.raise_for_status()
        catalog = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("HTTP error fetching catalog: %s", exc)
        return 1
    except httpx.RequestError as exc:
        logger.error("Network error fetching catalog: %s", exc)
        return 1
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON from catalog endpoint: %s", exc)
        return 1

    events = catalog.get("events", {})
    if not events:
        logger.warning("Catalog returned empty events dict — nothing to snapshot.")
        return 1

    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT_PATH.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "Wrote snapshot with %d event(s) (version=%s) to %s",
        len(events),
        catalog.get("version", "?"),
        _SNAPSHOT_PATH,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
