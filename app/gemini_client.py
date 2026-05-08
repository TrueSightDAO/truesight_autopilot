"""Gemini vision client for image analysis (QR codes, barcodes, cacao bags).

Used as a fallback after Grok vision in the QR scanner pipeline:
  Images → pyzbar → zbarimg → Grok vision → Gemini vision → result
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("autopilot.gemini")

GEMINI_MODEL = "gemini-2.0-flash-exp"

# Prompt for QR/barcode reading — mirrors grok_client.py's _QR_NAMING_CONTEXT
_GEMINI_VISION_PROMPT = """\
You are analyzing a product photo of a cacao bag from the Agroverse supply chain.
The photo was taken by a DAO governor to record a physical inventory transfer.

Read any visible QR codes, barcodes, or alphanumeric product codes in this image.
Look for Agroverse QR code patterns like 2024OSCAR_20260330_33 or LA_CC_20260414_1.

Agroverse QR codes appear as a URL or code on a white label:
  https://edgar.truesight.me/agroverse/qr-code-check?qr_code=<CODE>
CODE formats: "2024OSCAR_20260330_33" (legacy) or "LA_CC_20260414_1" (regional).
Extract just the CODE portion after "qr_code=".

Return EXACTLY ONE JSON object (no markdown, no code fences). Keys:
- "image_description": string — scene description, lighting, angle
- "product_type_guess": string or null — e.g. "Ceremonial Cacao Kraft Pouch"
- "label_text_visible": array of strings — printed text on labels
- "qr_codes_guessed": array of {data: string, confidence: number} — QR codes you THINK you see, with confidence 0.0-1.0. Empty array if unreadable.
- "barcodes_guessed": array of {type: string, data: string, confidence: number} — other barcodes (EAN, UPC, etc.)
- "qr_label_present": boolean or null — is a QR code label visible on the bag?
- "photo_quality": string — "clear", "blurry", "dark", "glare", "angled", "good"
- "notes": string — recommendations for the operator
"""


def load_gemini_key() -> str | None:
    """Resolve Gemini API key from environment or market_research/.env."""
    k = (os.environ.get("GEMINI_API_KEY") or "").strip()
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
            if line.startswith("GEMINI_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _load_google_credentials() -> str | None:
    """Try to load Google service account credentials JSON path."""
    candidates = [
        Path.home() / "Applications" / "market_research" / "google_credentials.json",
        Path("/Users/garyjob/Applications/market_research/google_credentials.json"),
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def gemini_analyze_image(
    image_path: str,
    prompt: str = "",
) -> dict[str, Any]:
    """Send a single image to Gemini for vision analysis.

    Args:
        image_path: Absolute file path to a JPEG/PNG image.
        prompt: Optional custom prompt. Uses default QR/barcode prompt if empty.

    Returns:
        dict with keys: status, text_response, qr_codes_guessed, barcodes_guessed, ...
    """
    api_key = load_gemini_key()
    if not api_key:
        return {
            "status": "error",
            "message": "GEMINI_API_KEY not found. Set it in environment or market_research/.env.",
        }

    p = Path(image_path)
    if not p.exists():
        return {
            "status": "error",
            "message": f"Image file not found: {image_path}",
        }

    try:
        import google.generativeai as genai
    except ImportError:
        return {
            "status": "error",
            "message": "google-generativeai package not installed. Run: pip install google-generativeai",
        }

    # Configure API key
    genai.configure(api_key=api_key)

    # Try service account credentials as fallback
    creds_path = _load_google_credentials()
    if creds_path:
        try:
            import google.auth
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            genai.configure(credentials=credentials)
        except Exception as e:
            logger.debug("Failed to load Google credentials from %s: %s", creds_path, e)

    # Read and encode image
    try:
        img_bytes = p.read_bytes()
        if len(img_bytes) < 20 * 1024:
            return {
                "status": "error",
                "message": f"Image too small ({len(img_bytes)} bytes): {image_path}",
            }
        if len(img_bytes) > 10 * 1024 * 1024:
            return {
                "status": "error",
                "message": f"Image too large ({len(img_bytes)} bytes): {image_path}",
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to read image: {e}",
        }

    # Build content parts
    effective_prompt = prompt or _GEMINI_VISION_PROMPT
    image_data = base64.b64encode(img_bytes).decode("ascii")
    mime_type = "image/jpeg"
    if p.suffix.lower() in (".png",):
        mime_type = "image/png"
    elif p.suffix.lower() in (".webp",):
        mime_type = "image/webp"

    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(
            [
                effective_prompt,
                {"mime_type": mime_type, "data": image_data},
            ],
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=2048,
            ),
        )
        text = response.text
        return _parse_gemini_response(text)
    except Exception as e:
        logger.error("Gemini API call failed for %s: %s", image_path, e)
        return {
            "status": "error",
            "message": f"Gemini API error: {e}",
        }


def _parse_gemini_response(text: str) -> dict[str, Any]:
    """Extract JSON from Gemini's response, handling markdown code fences."""
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
        result["text_response"] = text
        return result
    except json.JSONDecodeError:
        logger.warning("Failed to parse Gemini JSON response: %s", text[:500])
        return {
            "status": "parse_error",
            "message": "Could not parse Gemini response as JSON.",
            "raw_response": text[:1000],
            "text_response": text,
            "qr_codes_guessed": [],
            "barcodes_guessed": [],
        }
