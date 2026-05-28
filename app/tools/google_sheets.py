"""Read-only Google Sheets tool for the autopilot agent.

Exposes ``read_google_sheet(spreadsheet_id, range_a1, service_account_name=None)``
returning a JSON-serialisable dict with row data. Backed by the Sheets v4 API
via ``google-api-python-client``.

The default service account is whatever ``GOOGLE_APPLICATION_CREDENTIALS``
points at (typically ``cypher_defense_gdrive_key.json``, which has Viewer on
the Main Ledger). Pass ``service_account_name`` to switch — e.g. ``"tdg_scoring"``
for sheets only the scoring SA can see.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .google_creds import load_credentials

logger = logging.getLogger("autopilot.tools.google_sheets")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_MAX_CELLS = 5000  # cap rows*cols returned to keep tool output bounded


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def read_google_sheet(
    spreadsheet_id: str,
    range_a1: str,
    service_account_name: str | None = None,
) -> str:
    """Read a range from a Google Sheet. Returns JSON-string tool result."""
    if not spreadsheet_id or not range_a1:
        return _err("spreadsheet_id and range_a1 are required")

    creds = load_credentials(service_account_name, SHEETS_SCOPES)
    if creds is None:
        return _err("credentials missing", service_account_name=service_account_name)

    try:
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:  # pragma: no cover
        return _err(f"google-api-python-client unavailable: {e}")

    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_a1)
            .execute()
        )
    except Exception as e:
        logger.warning("read_google_sheet failed: %s", e)
        return _err(str(e), spreadsheet_id=spreadsheet_id, range=range_a1)

    values = resp.get("values", []) or []
    # Cap on (rows × cols) to avoid swamping the model context.
    total_cells = sum(len(r) for r in values)
    truncated = False
    if total_cells > _MAX_CELLS:
        capped: list[list[Any]] = []
        cells = 0
        for row in values:
            if cells + len(row) > _MAX_CELLS:
                room = _MAX_CELLS - cells
                if room > 0:
                    capped.append(row[:room])
                truncated = True
                break
            capped.append(row)
            cells += len(row)
        values = capped

    logger.info(
        "read_google_sheet ok: sheet=%s range=%s rows=%d truncated=%s",
        spreadsheet_id, range_a1, len(values), truncated,
    )
    return json.dumps({
        "status": "ok",
        "spreadsheet_id": spreadsheet_id,
        "range": resp.get("range", range_a1),
        "row_count": len(values),
        "values": values,
        "truncated": truncated,
    })
