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


def read_local_file(file_path: str) -> dict[str, Any]:
    """Read a local text file and return its contents.

    Args:
        file_path: Full path to the file on disk.

    Returns:
        {"status": "success", "content": "...", "path": "...", "size": N}
        or {"status": "error", "message": "..."}
    """
    # Sanitize: reject paths with ".." to prevent directory traversal
    if ".." in file_path.split(os.sep):
        return {"status": "error", "message": "Path traversal detected: '..' is not allowed."}

    p = Path(file_path)
    if not p.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}
    if not p.is_file():
        return {"status": "error", "message": f"Path is not a file: {file_path}"}

    # Reject binary files (check first 8KB for null bytes)
    try:
        chunk = p.read_bytes()[:8192]
        if b'\x00' in chunk:
            return {"status": "error", "message": "Binary file detected — use list_directory instead"}
    except Exception as e:
        return {"status": "error", "message": f"Error reading file: {e}"}

    try:
        content = p.read_text(encoding="utf-8")
        return {
            "status": "success",
            "content": content,
            "path": str(p.resolve()),
            "size": len(content),
        }
    except UnicodeDecodeError:
        return {"status": "error", "message": "File is not valid UTF-8 text"}
    except PermissionError:
        return {"status": "error", "message": f"Permission denied: {file_path}"}
    except OSError as e:
        return {"status": "error", "message": f"Error reading file: {e}"}


# ── capability manifest entries ───────────────────────────────────────────

import json as _json  # noqa: E402
from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="list_directory",
        description="List files in a local directory on the server.",
        parameters={
            "type": "object",
            "properties": {"dir_path": {"type": "string", "description": "Full path to the directory."}},
            "required": ["dir_path"],
        },
        handler=lambda args, ctx: _json.dumps(list_directory(args.get("dir_path", "")), indent=2),
    ),
    ToolSpec(
        name="read_local_file",
        description="Read a local text file from the server filesystem.",
        parameters={
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "Full path to the file."}},
            "required": ["file_path"],
        },
        handler=lambda args, ctx: _json.dumps(read_local_file(args.get("file_path", "")), indent=2),
    ),
]
