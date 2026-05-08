"""Grok vision client for image analysis (QR codes, barcodes, cacao bags).

Used as stage 1 of the two-stage autopilot pipeline:
  Images → Grok (vision) → structured analysis → DeepSeek (DAO context) → response
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("autopilot.grok")

GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4-1-fast-non-reasoning"  # fast, vision-capable

# Agile QR naming conventions to include in the vision prompt
_QR_NAMING_CONTEXT = """\
You are analyzing product photos of cacao bags from the Agroverse supply chain.
The photos were taken by a DAO governor to record a physical inventory transfer.

## Your role
Provide visual context AND attempt to read any QR codes or barcodes visible.
HOWEVER, you MUST mark QR code readings with a confidence level because
vision models often misread blurry or angled codes. A separate barcode library
(pyzbar) will also scan the image — your reading is a second opinion.

## How to read QR codes
Agroverse QR codes appear as a URL or code on a white label:
  https://edgar.truesight.me/agroverse/qr-code-check?qr_code=<CODE>
CODE formats: "2024OSCAR_20260330_33" (legacy) or "LA_CC_20260414_1" (regional).
Extract just the CODE portion after "qr_code=".

## What to report
- Describe the scene: bag type, angle, lighting
- Read ANY visible text on labels (farm names, weights, ingredients)
- Try to read QR codes/barcodes — but give a confidence (0.0-1.0) for EACH code
- If the photo is too blurry to read codes, say so explicitly
- Note where QR code labels are positioned on the bag

Return EXACTLY ONE JSON object (no markdown, no code fences). Keys:
- "image_description": string — scene description, lighting, angle
- "product_type_guess": string or null — e.g. "Ceremonial Cacao Kraft Pouch"
- "label_text_visible": array of strings — printed text on labels (farm, weight, ingredients)
- "bag_count_estimate": integer or null
- "qr_codes_guessed": array of {data: string, confidence: number} — QR codes you THINK you see, with confidence 0.0-1.0. Empty array if unreadable.
- "barcodes_guessed": array of {type: string, data: string, confidence: number} — other barcodes (EAN, UPC, etc.)
- "qr_label_present": boolean or null — is a QR code label visible on the bag?
- "qr_label_location": string or null — where on the bag (e.g. "back center sticker", "front bottom")
- "photo_quality": string — "clear", "blurry", "dark", "glare", "angled", "good"
- "notes": string — recommendations for the operator (e.g. "retake photo closer to QR label", "QR label partially obscured by glare")"""


def load_grok_key() -> str | None:
    """Resolve Grok API key from environment or market_research/.env."""
    k = (os.environ.get("GROK_API_KEY") or "").strip()
    if k:
        return k

    # Fallback: read from market_research/.env
    candidates = [
        Path.home() / "Applications" / "market_research" / ".env",
        Path(__file__).resolve().parent.parent.parent / "market_research" / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GROK_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def grok_analyze_images(
    image_paths: list[str],
    user_context: str = "",
    model: str = GROK_MODEL,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Send images to Grok for vision analysis (visual context only, NO barcode reading).

    Args:
        image_paths: List of absolute file paths to JPEG/PNG images.
        user_context: Additional context from the user's prompt.
        model: Grok model to use.
        temperature: LLM temperature.
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON dict with keys: image_description, product_type_guess,
        label_text_visible, bag_count_estimate, qr_label_location, photo_quality, notes.
    """
    api_key = load_grok_key()
    if not api_key:
        return {
            "status": "error",
            "message": "GROK_API_KEY not found. Set it in environment or market_research/.env.",
        }

    # Build user prompt with image parts
    user_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"Analyze these photos.\n\n{user_context}" if user_context else "Analyze these photos.",
        }
    ]

    for fp in image_paths:
        p = Path(fp)
        if not p.exists():
            continue
        try:
            img_bytes = p.read_bytes()
            if not (20 * 1024 <= len(img_bytes) <= 10 * 1024 * 1024):
                continue  # skip too-small or too-large files
        except Exception:
            continue

        user_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{_b64(img_bytes)}", "detail": "high"},
            }
        )

    if len(user_parts) < 2:
        return {
            "status": "error",
            "message": "No valid images to analyze.",
        }

    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": _QR_NAMING_CONTEXT},
            {"role": "user", "content": user_parts},
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(GROK_ENDPOINT, headers=headers, json=payload)
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse_grok_response(content)
    except httpx.HTTPStatusError as exc:
        logger.error("Grok API error %s: %s", exc.response.status_code, exc.response.text[:500])
        return {
            "status": "error",
            "message": f"Grok API error {exc.response.status_code}",
        }
    except Exception as exc:
        logger.error("Grok request failed: %s", exc)
        return {
            "status": "error",
            "message": str(exc),
        }


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _parse_grok_response(text: str) -> dict[str, Any]:
    """Extract JSON from Grok's response, handling markdown code fences."""
    text = text.strip()
    # Remove code fences if present
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            text = m.group(0)

    try:
        result = json.loads(text)
        result["status"] = "success"
        return result
    except json.JSONDecodeError:
        logger.warning("Failed to parse Grok JSON response: %s", text[:500])
        return {
            "status": "parse_error",
            "message": "Could not parse Grok response as JSON.",
            "raw_response": text[:1000],
        }


def grok_analyze_batch(
    image_paths: list[str],
    user_context: str = "",
    max_images_per_call: int = 10,
) -> dict[str, Any]:
    """Analyze a potentially large batch by chunking into manageable calls."""
    if len(image_paths) <= max_images_per_call:
        return grok_analyze_images(image_paths, user_context=user_context)

    # Chunk and aggregate
    all_qr_guesses: list[dict] = []
    all_bc_guesses: list[dict] = []
    descriptions: list[str] = []
    all_label_text: list[str] = []
    all_notes: list[str] = []

    for i in range(0, len(image_paths), max_images_per_call):
        chunk = image_paths[i : i + max_images_per_call]
        ctx = f"Batch {i // max_images_per_call + 1}/{(len(image_paths) + max_images_per_call - 1) // max_images_per_call}. {user_context}"
        result = grok_analyze_images(chunk, user_context=ctx)

        if result.get("status") == "success":
            all_qr_guesses.extend(result.get("qr_codes_guessed", []))
            all_bc_guesses.extend(result.get("barcodes_guessed", []))
            if desc := result.get("image_description", ""):
                descriptions.append(desc)
            all_label_text.extend(result.get("label_text_visible", []))
            if notes := result.get("notes", ""):
                all_notes.append(notes)

    return {
        "status": "success",
        "image_description": " | ".join(descriptions),
        "product_type_guess": result.get("product_type_guess"),
        "label_text_visible": all_label_text,
        "bag_count_estimate": len(image_paths),
        "qr_codes_guessed": all_qr_guesses,
        "barcodes_guessed": all_bc_guesses,
        "qr_label_present": result.get("qr_label_present"),
        "qr_label_location": result.get("qr_label_location"),
        "photo_quality": result.get("photo_quality"),
        "notes": " | ".join(all_notes) if all_notes else "",
    }
