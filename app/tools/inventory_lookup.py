"""QR cache lookup tools for the autopilot.

Provides list_matching_qr_codes(prefix) to search previously looked-up
QR codes from the local JSON cache.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.inventory_lookup")

_QR_CACHE_PATH = Path("/tmp/autopilot_qr_cache.json")


def _load_cache() -> dict[str, Any]:
    """Load the QR cache from disk. Returns empty dict if missing or corrupt."""
    if not _QR_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_QR_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception as e:
        logger.warning("Failed to load QR cache: %s", e)
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    """Save the QR cache to disk atomically."""
    try:
        _QR_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save QR cache: %s", e)


def _cache_qr_result(qr_code: str, result: dict[str, Any]) -> None:
    """Persist a single QR lookup result to the cache (append-only)."""
    cache = _load_cache()
    cache[qr_code] = result
    _save_cache(cache)


def list_matching_qr_codes(prefix: str) -> dict[str, Any]:
    """Search the QR cache for codes starting with the given prefix.

    Args:
        prefix: The prefix to search for (e.g. '2024OSCAR_2026' or 'LA').

    Returns:
        {"status": "success", "prefix": "...", "matches": [...], "count": N}
        or {"status": "error", "message": "..."}
    """
    if not prefix:
        return {"status": "error", "message": "Prefix is required."}

    try:
        cache = _load_cache()
    except Exception as e:
        return {"status": "error", "message": f"Failed to load cache: {e}"}

    matches = []
    for code, record in cache.items():
        if code.startswith(prefix):
            matches.append({
                "qr_code": code,
                "status": record.get("status", ""),
                "manager": record.get("manager_name", ""),
                "owner": record.get("owner_name", ""),
                "currency": record.get("currency", ""),
            })

    return {
        "status": "success",
        "prefix": prefix,
        "matches": matches,
        "count": len(matches),
    }


# ── capability manifest entry ─────────────────────────────────────────────

import json as _json  # noqa: E402
from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="list_matching_qr_codes",
    description="Search previously looked-up QR codes by prefix.",
    parameters={
        "type": "object",
        "properties": {"prefix": {"type": "string", "description": "QR code prefix to match."}},
        "required": ["prefix"],
    },
    handler=lambda args, ctx: _json.dumps(list_matching_qr_codes(args.get("prefix", "")), indent=2),
)
