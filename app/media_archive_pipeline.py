"""Media Archives Pipeline dashboard — read-only data endpoint.

Serves queue state for the MAP (Media Archives Pipeline): per-farm counts and
items (uploaded / pending / needs_metadata / error), upload events, and the
committed manifest index. Auth-gated: requires a valid governor JWT.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .auth import verify_jwt

router = APIRouter()

INBOX_ROOT = Path(
    os.environ.get("MEDIA_ARCHIVE_INBOX", "/home/ubuntu/media_archive_inbox")
)
UPLOAD_LOG = Path(
    os.environ.get("FARM_MEDIA_UPLOAD_LOG", "/tmp/farm_media_uploads.log")
)
MANIFEST_INDEX_URL = "https://raw.githubusercontent.com/TrueSightDAO/agentic_ai_context/main/FARM_MEDIA_MANIFESTS/index.json"

VALID_SOURCES = {"farm-media", "event-media", "partner-media"}


def _parse_sidecar(path: Path) -> dict[str, Any]:
    """Parse a sidecar JSON defensively — never hard-crash on schema drift."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _status_of(sidecar: dict[str, Any]) -> str:
    yt_id = sidecar.get("yt_id")
    err = sidecar.get("error")
    if yt_id:
        return "uploaded"
    if err:
        return "error"
    # pending but metadata-incomplete
    required = ("sha256", "gps", "title")
    if any(not sidecar.get(k) for k in required):
        return "needs_metadata"
    return "pending"


def _scan_inbox() -> list[dict[str, Any]]:
    """Scan inbox root for <source>/<farm_id>/*.json sidecars."""
    farms: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not INBOX_ROOT.exists():
        return []
    for source_dir in sorted(p for p in INBOX_ROOT.iterdir() if p.is_dir()):
        source = source_dir.name
        if source not in VALID_SOURCES:
            continue
        for farm_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            farm_id = farm_dir.name
            key = (source, farm_id)
            farms.setdefault(key, [])
            for sidecar_path in sorted(farm_dir.glob("*.json")):
                sidecar = _parse_sidecar(sidecar_path)
                if not sidecar:
                    continue
                sidecar["_path"] = str(sidecar_path)
                farms[key].append(sidecar)
    result = []
    for (source, farm_id), items in sorted(farms.items()):
        counts = {"uploaded": 0, "pending": 0, "needs_metadata": 0, "error": 0}
        for it in items:
            counts[_status_of(it)] += 1
        result.append(
            {
                "source": source,
                "farm_id": farm_id,
                "counts": counts,
                "total": len(items),
                "items": items,
            }
        )
    return result


def _read_upload_log(limit: int = 200) -> list[dict[str, str]]:
    """Tail the upload log into structured events (defensive parse)."""
    events: list[dict[str, str]] = []
    if not UPLOAD_LOG.exists():
        return events
    try:
        lines = UPLOAD_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return events
    for line in lines[-limit:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        events.append(
            {
                "ts": f"{parts[0]} {parts[1]}",
                "farm_id": parts[2],
                "file": parts[3].rstrip(":"),
                "result": " ".join(parts[4:]),
            }
        )
    return events


def _fetch_manifest_index() -> dict[str, Any]:
    """Fetch the committed manifest index (GitHub) — defensive."""
    try:
        import urllib.request

        with urllib.request.urlopen(MANIFEST_INDEX_URL, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/media-archive-pipeline/data")
def media_archive_pipeline_data(request: Request) -> dict[str, Any]:
    """Auth-gated queue state for the MAP dashboard."""
    verify_jwt(request)  # raises 401 without a valid governor token
    try:
        farms = _scan_inbox()
        events = _read_upload_log()
        index = _fetch_manifest_index()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "farms": farms,
            "upload_events": events,
            "manifest_index": index,
        }
    except Exception as exc:  # never 500 with raw internals
        raise HTTPException(
            status_code=500, detail=f"pipeline data error: {exc}"
        ) from exc
