"""Image generation via Gemini (Google AI Studio) — one operation.

- ``generate_image(prompt, filename=None, aspect_ratio=None)`` — generates an
  image from a text prompt and saves it to ``/tmp/tg_attachments/`` (the
  same shared directory the Telegram adapter already uses for incoming/
  outgoing files), returning the local path. Chain with the existing
  ``send_telegram_attachment`` tool to actually deliver it.

Credentials: ``GEMINI_API_KEY`` env var. No key configured degrades to a
clean error, not a crash.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path

import requests

logger = logging.getLogger("autopilot.tools.image_generation")

_MODEL = "gemini-2.5-flash-image"
_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
_ATTACH_DIR = "/tmp/tg_attachments"  # same shared dir telegram_adapter.py uses
_TIMEOUT = 60


def _err(reason: str, **extra) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def generate_image(prompt: str, filename: str | None = None, aspect_ratio: str | None = None) -> str:
    if not prompt:
        return _err("prompt is required")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return _err("GEMINI_API_KEY not configured")

    body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
    if aspect_ratio:
        body["generationConfig"] = {"imageConfig": {"aspectRatio": aspect_ratio}}

    try:
        resp = requests.post(
            _API_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json=body,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        return _err(f"Gemini request failed: {e}")

    if resp.status_code != 200:
        return _err(f"Gemini API returned HTTP {resp.status_code}", detail=resp.text[:500])

    data = resp.json()
    parts = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )
    image_part = next((p for p in parts if "inlineData" in p), None)
    if image_part is None:
        return _err("Gemini response had no image data", detail=json.dumps(data)[:500])

    inline = image_part["inlineData"]
    mime_type = inline.get("mimeType", "image/png")
    ext = mime_type.split("/")[-1] if "/" in mime_type else "png"

    Path(_ATTACH_DIR).mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = f"generated_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    elif not filename.endswith(f".{ext}"):
        filename = f"{filename}.{ext}"

    out_path = Path(_ATTACH_DIR) / filename
    try:
        out_path.write_bytes(base64.b64decode(inline["data"]))
    except Exception as e:
        return _err(f"failed to write image file: {e}")

    logger.info("generate_image ok: path=%s bytes=%d", out_path, out_path.stat().st_size)
    return json.dumps(
        {
            "status": "ok",
            "path": str(out_path),
            "mime_type": mime_type,
            "bytes": out_path.stat().st_size,
        }
    )


# ── capability manifest entries ───────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPECS = [
    ToolSpec(
        name="generate_image",
        description=(
            "Generate an image from a text prompt (Gemini / Google AI Studio) and save it "
            "locally. Returns the file path — chain with send_telegram_attachment to deliver "
            "it, or with upload_file_to_github / gmail_send / calendar_create_event's "
            "attachments to use it elsewhere. Do not hand-roll a raw API call to Gemini for "
            "this — always use this tool so credential handling and file conventions stay "
            "consistent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the image to generate."},
                "filename": {"type": "string", "description": "Optional filename (extension inferred from the response if omitted)."},
                "aspect_ratio": {"type": "string", "description": "Optional aspect ratio, e.g. '1:1', '16:9', '9:16'."},
            },
            "required": ["prompt"],
        },
        handler=lambda args, ctx: generate_image(
            prompt=args.get("prompt", ""),
            filename=args.get("filename"),
            aspect_ratio=args.get("aspect_ratio"),
        ),
    ),
]
