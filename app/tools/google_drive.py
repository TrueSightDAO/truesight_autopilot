"""Read-only Google Drive tools for the autopilot agent.

- ``read_drive_file(file_id, mime_type=None, service_account_name=None)`` —
  download or export the file content. Google-native types (Docs/Sheets/Slides)
  are auto-exported to a text-friendly format unless the caller forces a
  ``mime_type``. Binary blobs are returned base64-encoded.
- ``list_drive_folder(folder_id, page_size=50, service_account_name=None)`` —
  list direct children of a folder with id/name/mimeType/size/modifiedTime.

Output sizes are capped to keep tool replies bounded.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from .google_creds import load_credentials

logger = logging.getLogger("autopilot.tools.google_drive")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_MAX_BYTES = 256 * 1024  # 256 KB cap on returned file body

# Google native MIME → best-effort text export mime.
_NATIVE_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing": "image/png",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _build_service(service_account_name: str | None):
    creds = load_credentials(service_account_name, DRIVE_SCOPES)
    if creds is None:
        return None, _err(
            "credentials missing", service_account_name=service_account_name
        )
    try:
        from googleapiclient.discovery import build  # type: ignore

        return build("drive", "v3", credentials=creds, cache_discovery=False), None
    except Exception as e:  # pragma: no cover
        return None, _err(f"google-api-python-client unavailable: {e}")


def read_drive_file(
    file_id: str,
    mime_type: str | None = None,
    service_account_name: str | None = None,
) -> str:
    """Download (or export) a Drive file's content."""
    if not file_id:
        return _err("file_id is required")

    service, err = _build_service(service_account_name)
    if service is None:
        return err  # type: ignore[return-value]

    try:
        meta = (
            service.files()
            .get(fileId=file_id, fields="id,name,mimeType,size,modifiedTime")
            .execute()
        )
    except Exception as e:
        return _err(str(e), file_id=file_id)

    file_mime = meta.get("mimeType", "")
    is_native = file_mime.startswith("application/vnd.google-apps.")
    try:
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore

        buf = io.BytesIO()
        if is_native:
            export_mime = mime_type or _NATIVE_EXPORTS.get(file_mime, "text/plain")
            req = service.files().export_media(fileId=file_id, mimeType=export_mime)
            effective_mime = export_mime
        else:
            req = service.files().get_media(fileId=file_id)
            effective_mime = mime_type or file_mime or "application/octet-stream"
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buf.tell() > _MAX_BYTES:
                break
    except Exception as e:
        return _err(str(e), file_id=file_id)

    raw = buf.getvalue()
    truncated = len(raw) > _MAX_BYTES
    if truncated:
        raw = raw[:_MAX_BYTES]

    # Try to decode as text; fall back to base64 for binary.
    if effective_mime.startswith("text/") or effective_mime in {
        "application/json",
        "application/csv",
    }:
        try:
            content = raw.decode("utf-8")
            encoding = "text"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
    else:
        content = base64.b64encode(raw).decode("ascii")
        encoding = "base64"

    logger.info(
        "read_drive_file ok: id=%s mime=%s bytes=%d truncated=%s",
        file_id,
        effective_mime,
        len(raw),
        truncated,
    )
    return json.dumps(
        {
            "status": "ok",
            "file_id": file_id,
            "name": meta.get("name"),
            "source_mime_type": file_mime,
            "effective_mime_type": effective_mime,
            "encoding": encoding,
            "content": content,
            "byte_count": len(raw),
            "truncated": truncated,
            "modified_time": meta.get("modifiedTime"),
        }
    )


def list_drive_folder(
    folder_id: str,
    page_size: int = 50,
    service_account_name: str | None = None,
) -> str:
    """List direct children of a Drive folder."""
    if not folder_id:
        return _err("folder_id is required")

    service, err = _build_service(service_account_name)
    if service is None:
        return err  # type: ignore[return-value]

    page_size = max(1, min(int(page_size or 50), 200))
    try:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=page_size,
                fields="files(id, name, mimeType, size, modifiedTime), nextPageToken",
            )
            .execute()
        )
    except Exception as e:
        return _err(str(e), folder_id=folder_id)

    files = resp.get("files", []) or []
    logger.info("list_drive_folder ok: folder=%s files=%d", folder_id, len(files))
    return json.dumps(
        {
            "status": "ok",
            "folder_id": folder_id,
            "files": files,
            "next_page_token": resp.get("nextPageToken"),
        }
    )


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="read_drive_file",
        description="Download (or export) a Google Drive file's content. Google-native types (Docs, Sheets, Slides) are auto-exported to text/csv/plain unless you force mime_type. Binary blobs returned base64-encoded. Capped at 256KB.",
        parameters={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "The Drive file ID."},
                "mime_type": {
                    "type": "string",
                    "description": "Optional explicit export/download MIME type.",
                },
                "service_account_name": {
                    "type": "string",
                    "description": "Optional SA name (see read_google_sheet).",
                },
            },
            "required": ["file_id"],
        },
        handler=lambda args, ctx: read_drive_file(
            file_id=args.get("file_id", ""),
            mime_type=args.get("mime_type"),
            service_account_name=args.get("service_account_name"),
        ),
    ),
    ToolSpec(
        name="list_drive_folder",
        description="List direct children of a Google Drive folder (id, name, mimeType, size, modifiedTime).",
        parameters={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string", "description": "The Drive folder ID."},
                "page_size": {
                    "type": "integer",
                    "description": "Max files to return (1-200). Default 50.",
                    "default": 50,
                },
                "service_account_name": {
                    "type": "string",
                    "description": "Optional SA name (see read_google_sheet).",
                },
            },
            "required": ["folder_id"],
        },
        handler=lambda args, ctx: list_drive_folder(
            folder_id=args.get("folder_id", ""),
            page_size=args.get("page_size", 50),
            service_account_name=args.get("service_account_name"),
        ),
    ),
]
