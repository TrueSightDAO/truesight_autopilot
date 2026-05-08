"""Filesystem tools for the autopilot.

Provides tools to discover and inspect files on the local server filesystem.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.fs_tools")


def list_directory(dir_path: str) -> dict[str, Any]:
    """List files in a local directory on the server.

    Returns file names, sizes, and types. Use this to discover files
    before scanning QR codes or reading them.

    Args:
        dir_path: Full path to the directory to list.

    Returns:
        {"files": [{"name": ..., "path": ..., "size": ..., "is_dir": ..., "ext": ...}],
         "count": N}
        or {"status": "error", "message": "..."}
    """
    # Sanitize: reject paths with ".." to prevent directory traversal
    if ".." in dir_path.split(os.sep):
        return {"status": "error", "message": "Path traversal detected: '..' is not allowed."}

    p = Path(dir_path)

    if not p.exists():
        return {"status": "error", "message": f"Directory not found: {dir_path}"}

    if not p.is_dir():
        return {"status": "error", "message": f"Path is not a directory: {dir_path}"}

    try:
        entries = []
        for entry in p.iterdir():
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                    "size": stat.st_size,
                    "is_dir": entry.is_dir(),
                    "ext": entry.suffix.lower() if not entry.is_dir() else "",
                })
            except (OSError, PermissionError) as e:
                # Skip entries we can't stat (permission denied, broken symlinks, etc.)
                entries.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                    "size": 0,
                    "is_dir": False,
                    "ext": "",
                    "error": str(e),
                })

        # Sort: directories first, then by name
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

        return {
            "files": entries,
            "count": len(entries),
            "directory": str(p.resolve()),
        }

    except PermissionError:
        return {"status": "error", "message": f"Permission denied: {dir_path}"}
    except OSError as e:
        return {"status": "error", "message": f"Error reading directory: {e}"}
