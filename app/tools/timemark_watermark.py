"""Timemark watermark reader — detect/parse the burned-in field-photo watermark.

Field photos from the cacao planting app carry a *burned-in* watermark (not
EXIF, so it survives Telegram/Discord recompression):

    bottom-left : "<wd>, <d> <mon> <yyyy> <HH:MM>" / "<place>" / "<lat>°S, <lon>°W"
    right edge  : "<CODE> Timemark Verified"   (rotated 90°)

The CODE is the source app's per-capture unique id — the strongest join key for
dedup (unique per capture, immune to filename/date mislabeling).

Tesseract alone cannot read the light-on-foliage block, so this module:
  1. crops the two watermark regions,
  2. tries local OCR on the tight crops (fast, free),
  3. falls back to Gemini vision when a key is available,
  4. parses the date / GPS / code out of whichever text won.

Vision is injected (``vision_fn``) so callers/tests never require the network.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("timemark_watermark")

# 12-16 char uppercase alnum token that MUST contain >=1 letter and >=1 digit
# (so it can't be a bare GPS number or a date).
_CODE_RE = re.compile(
    r"\b(?=[A-Z0-9]{12,16}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{12,16}\b"
)
# 3.522716°S, 51.574659°W  (degree symbol optional)
_GPS_RE = re.compile(
    r"(\d{1,2}\.\d{3,})\s*°?\s*([NS])\s*[, ]\s*(\d{1,3}\.\d{3,})\s*°?\s*([EW])",
    re.IGNORECASE,
)
# Seg, 12 out. 2026 07:14   (weekday, day month year HH:MM)
_DATE_RE = re.compile(
    r"\b\w{3,9},?\s*\d{1,2}\s+[\wç\.]{3,6}\.?\s+\d{4}\s+\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
# A short place line, e.g. "Pará" (kept loose; only used for display).
_PLACE_RE = re.compile(r"^\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ ]{2,30})\s*$", re.MULTILINE)


def parse_timemark_text(text: str) -> dict:
    """Extract {capture_time, place, lat, lat_ref, lon, lon_ref, code} from text."""
    out: dict = {}
    if not text:
        return out
    t = text.replace("\u00b0", "°")

    m = _GPS_RE.search(t)
    if m:
        out["lat"], out["lat_ref"], out["lon"], out["lon_ref"] = m.groups()
    c = _CODE_RE.search(t.upper())
    if c:
        out["code"] = c.group(0)
    d = _DATE_RE.search(t)
    if d:
        out["capture_time"] = re.sub(r"\s+", " ", d.group(0)).strip()
    return out


def crop_watermark_regions(path: str) -> dict:
    """Return {'bottom': Image, 'right': Image} watermark crops (PIL images)."""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    w, h = im.size
    bottom = im.crop((0, int(h * 0.68), w, h))
    # right-edge vertical strip, un-rotated so it reads left-to-right
    right = im.crop((int(w * 0.84), 0, w, h)).rotate(-90, expand=True)
    return {"bottom": bottom, "right": right}


def _local_ocr(image) -> str:
    """Best-effort local tesseract on a preprocessed crop; '' on any failure."""
    try:
        import io

        import pytesseract
        from PIL import ImageEnhance, ImageOps

        g = image.convert("L")
        g = ImageEnhance.Contrast(g).enhance(2.0)
        g = ImageOps.autocontrast(g, cutoff=3)
        g = g.resize((g.width * 2, g.height * 2))
        buf = io.BytesIO()
        g.save(buf, format="PNG")
        buf.seek(0)
        return pytesseract.image_to_string(  # type: ignore[attr-defined]
            buf.getvalue(), config="--psm 6"
        )
    except Exception as exc:  # noqa: BLE001 - OCR is best-effort
        logger.debug("local watermark OCR failed: %s", exc)
        return ""


def _load_env_key(name: str) -> str | None:
    """Env var, else a best-effort read of the repo .env (CLI convenience)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        for line in env_path.read_text().splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


_PROMPT = (
    "This is the burned-in Timemark watermark of a Brazilian field photo, split "
    "into two crops: (1) the bottom strip (date/time, place, GPS) and (2) the "
    "right-edge strip un-rotated (a verification code followed by 'Timemark "
    "Verified'). Transcribe every visible token verbatim: the date/time, the "
    "place name, the GPS latitude/longitude, and the short uppercase alphanumeric "
    "verification code. Output plain text, one item per line. If a token is "
    "absent, omit it."
)


def _default_vision(regions: dict) -> str:
    """Gemini-vision fallback over the two crops; '' when no key / on failure."""
    key = _load_env_key("GEMINI_API_KEY") or _load_env_key("GOOGLE_API_KEY")
    if not key:
        return ""
    try:
        import base64
        import io

        import httpx

        parts: list[dict] = [{"text": _PROMPT}]
        for tag in ("bottom", "right"):
            img = regions.get(tag)
            if img is None:
                continue
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                    }
                }
            )
        model = os.environ.get("TIMEMARK_VISION_MODEL", "gemini-3.8-flash")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={key}"
        )
        resp = httpx.post(url, json={"contents": [{"parts": parts}]}, timeout=90)
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:  # noqa: BLE001 - vision must never be fatal
        logger.warning("timemark vision fallback failed: %s", exc)
        return ""


def format_summary(fields: dict) -> str:
    """One-line human/ledger summary from parsed fields."""
    bits = []
    if fields.get("capture_time"):
        bits.append(fields["capture_time"])
    if fields.get("place"):
        bits.append(fields["place"])
    if fields.get("lat") and fields.get("lon"):
        bits.append(
            f"{fields['lat']}°{fields.get('lat_ref', '')}, "
            f"{fields['lon']}°{fields.get('lon_ref', '')}".strip()
        )
    if fields.get("code"):
        bits.append(f"code {fields['code']}")
    return "[Timemark] " + " | ".join(bits) if bits else ""


def read_timemark_watermark(path: str, vision_fn=None, ocr_fn=None) -> dict:
    """Detect & read the Timemark watermark in ``path``.

    Returns a dict with any of capture_time/place/lat/lon/code plus 'method'
    ('tesseract' | 'vision') and a one-line 'summary'. Returns {} when nothing
    watermark-like is found (so callers can treat it as "no watermark").
    """
    try:
        regions = crop_watermark_regions(path)
    except Exception as exc:  # noqa: BLE001 - unreadable image -> no watermark
        logger.debug("timemark crop failed: %s", exc)
        return {}

    local = ocr_fn or _local_ocr
    text = " ".join(local(regions[tag]) or "" for tag in ("bottom", "right"))
    parsed = parse_timemark_text(text)
    if parsed.get("code") and (parsed.get("lat") or parsed.get("capture_time")):
        parsed["method"] = "tesseract"
        place = _PLACE_RE.search(text)
        if place and not parsed.get("place"):
            parsed["place"] = place.group(1).strip()
        parsed["summary"] = format_summary(parsed)
        return parsed

    vf = vision_fn or _default_vision
    try:
        vtext = vf(regions) or ""
    except Exception as exc:  # noqa: BLE001 - vision must never be fatal
        logger.warning("timemark vision_fn raised: %s", exc)
        vtext = ""
    vparsed = parse_timemark_text(vtext)
    merged = dict(parsed)
    for k, v in vparsed.items():
        if v:
            merged[k] = v
    if not merged.get("code") and not (
        merged.get("lat") and merged.get("capture_time")
    ):
        return {}
    merged["method"] = "vision"
    place = _PLACE_RE.search(vtext)
    if place and not merged.get("place"):
        merged["place"] = place.group(1).strip()
    merged["summary"] = format_summary(merged)
    return merged
