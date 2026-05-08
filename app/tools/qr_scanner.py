"""Barcode / QR code scanning and lookup tools for the autopilot.

Handles scanning QR codes, EAN/UPC barcodes, and other symbologies from
uploaded images (cacao bags, product labels, etc.) and looking up Agroverse
QR records via the dao_client GAS backend.

Decodes: QRCODE, EAN13, EAN8, UPC-A, UPC-E, CODE128, CODE39, ITF, etc.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from .inventory_lookup import _cache_qr_result

logger = logging.getLogger("autopilot.qr_scanner")


# Known Agroverse QR code prefixes (from AGROVERSE_QR_CODE_BATCH_GENERATION.md)
_AGROVERSE_QR_PATTERN = r"^(?:2[0-9]{3}[A-Z]+_\d{8}_\d+|LA\d+|CC\d+|CT\d+)"

# Size thresholds for resize optimization
_MAX_DIMENSION_FULL = 2000  # If larger than this, resize to 50% for faster decode


# ──────────────────────── Barcode / QR Decoding ─────────────────────

def _decode_pyzbar(image_path: str) -> list[dict[str, str]]:
    """Primary decoder: pyzbar (wraps libzbar). Returns list of {type, data}.

    Tries full resolution first, then 50% scale for better detection on
    large photos where the barcode might be small relative to the image.
    """
    from pyzbar.pyzbar import decode as zbar_decode
    img = Image.open(image_path)
    w, h = img.size

    all_codes: list[dict[str, str]] = []

    # Try multiple scales for better detection
    scales = [(w, h)]  # full resolution
    if max(w, h) > _MAX_DIMENSION_FULL:
        scales.append((w // 2, h // 2))  # 50%

    for sw, sh in scales:
        if (sw, sh) != (w, h):
            img_scaled = img.resize((sw, sh), Image.LANCZOS)
        else:
            img_scaled = img

        try:
            results = zbar_decode(img_scaled)
            for r in results:
                try:
                    data = r.data.decode("utf-8")
                except UnicodeDecodeError:
                    data = r.data.decode("latin-1")
                # Filter empty data (false positives from CODE128 etc.)
                if data.strip():
                    all_codes.append({"type": r.type, "data": data.strip()})
        except Exception:
            continue

        # If we found codes at this scale, skip further scales for this image
        if all_codes:
            break

    return all_codes


def _decode_zbarimg(image_path: str) -> list[dict[str, str]]:
    """Fallback: shell out to zbarimg CLI. Supports all types."""
    result = subprocess.run(
        ["zbarimg", "--quiet", "-Sdisable", "-Sqr.enable",
         "-Sean13.enable", "-Sean8.enable", "-Supca.enable", "-Supce.enable",
         "-Scode128.enable", "-Scode39.enable",
         "--raw", image_path],
        capture_output=True, text=False, timeout=15,
        env={**os.environ, "ZBAR_QUIET": "1"},
    )
    if result.returncode != 0:
        return []

    # zbarimg --raw outputs one code per line (no type info with --raw)
    codes: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            codes.append({"type": "unknown", "data": line})

    # Try again with -S to get type info for better output
    try:
        typed_result = subprocess.run(
            ["zbarimg", "--quiet", image_path],
            capture_output=True, text=False, timeout=15,
            env={**os.environ, "ZBAR_QUIET": "1"},
        )
        if typed_result.returncode == 0:
            # Parse typed output: "EAN-13:0860010660232"
            typed_codes: list[dict[str, str]] = []
            typed_seen: set[str] = set()
            for line in typed_result.stdout.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if ":" in line and line not in typed_seen:
                    sym, data = line.split(":", 1)
                    typed_seen.add(line)
                    typed_codes.append({"type": sym, "data": data})
            if typed_codes:
                return typed_codes
    except Exception:
        pass

    return codes


def scan_qr_from_file(file_path: str) -> dict[str, Any]:
    """Decode all barcodes / QR codes found in a single image file.

    Returns:
        {"status": "success", "codes": [{"type": "EAN13", "data": "0860..."}], "file": "..."}
        {"status": "no_code_found", "message": "...", "file": "..."}
        {"status": "error", "message": "...", "file": "..."}
    """
    p = Path(file_path)
    if not p.exists():
        return {"status": "error", "message": f"File not found: {file_path}", "file": file_path}

    # Auto-convert HEIC/HEIF to JPEG since pyzbar/PIL cannot read them directly
    decode_path = file_path
    if p.suffix.lower() in (".heic", ".heif"):
        jpg_path = _convert_heic_to_jpg(file_path)
        if jpg_path is None:
            return {
                "status": "error",
                "message": f"Failed to convert HEIC/HEIF file to JPEG: {file_path}",
                "file": file_path,
            }
        decode_path = jpg_path

    codes: list[dict[str, str]] = []

    # Try pyzbar first (faster, more reliable)
    try:
        codes = _decode_pyzbar(decode_path)
    except Exception as e:
        logger.debug("pyzbar decode failed for %s: %s", decode_path, e)

    # Fall back to zbarimg CLI
    if not codes:
        try:
            codes = _decode_zbarimg(decode_path)
        except Exception as e:
            logger.debug("zbarimg decode failed for %s: %s", decode_path, e)

    # Grok vision fallback: if no codes found and file is an image, try Grok
    if not codes:
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
        if Path(decode_path).suffix.lower() in image_exts:
            try:
                from app.grok_client import grok_analyze_images, GROK_MODEL

                grok_result = grok_analyze_images(
                    [decode_path],
                    user_context="",
                    model="grok-4-1",
                )
                if grok_result.get("status") == "success":
                    # Collect QR code guesses from Grok
                    grok_codes: list[dict[str, str]] = []
                    seen_grok: set[str] = set()
                    for guess in grok_result.get("qr_codes_guessed", []):
                        data = guess.get("data", "").strip()
                        if data and data not in seen_grok:
                            seen_grok.add(data)
                            grok_codes.append({"type": "grok_vision", "data": data})
                    # Also check barcodes_guessed
                    for guess in grok_result.get("barcodes_guessed", []):
                        data = guess.get("data", "").strip()
                        if data and data not in seen_grok:
                            seen_grok.add(data)
                            grok_codes.append({"type": "grok_vision", "data": data})
                    if grok_codes:
                        codes = grok_codes
            except Exception as e:
                logger.debug("Grok vision fallback failed for %s: %s", decode_path, e)

    # Gemini vision fallback: if no codes found after Grok, try Gemini
    if not codes:
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
        if Path(decode_path).suffix.lower() in image_exts:
            try:
                from app.gemini_client import gemini_analyze_image

                gemini_result = gemini_analyze_image(
                    decode_path,
                    prompt=(
                        "Read any visible QR codes, barcodes, or alphanumeric product codes "
                        "in this image. Look for Agroverse QR code patterns like "
                        "2024OSCAR_20260330_33 or LA_CC_20260414_1. "
                        "Return them with confidence levels."
                    ),
                )
                if gemini_result.get("status") == "success":
                    gemini_codes: list[dict[str, str]] = []
                    seen_gemini: set[str] = set()
                    for guess in gemini_result.get("qr_codes_guessed", []):
                        data = guess.get("data", "").strip()
                        if data and data not in seen_gemini:
                            seen_gemini.add(data)
                            gemini_codes.append({"type": "gemini_vision", "data": data})
                    for guess in gemini_result.get("barcodes_guessed", []):
                        data = guess.get("data", "").strip()
                        if data and data not in seen_gemini:
                            seen_gemini.add(data)
                            gemini_codes.append({"type": "gemini_vision", "data": data})
                    if gemini_codes:
                        codes = gemini_codes
            except Exception as e:
                logger.debug("Gemini vision fallback failed for %s: %s", decode_path, e)

    if not codes:
        return {
            "status": "no_code_found",
            "message": "No barcodes or QR codes detected in this image.",
            "codes": [],
            "file": str(p),
        }

    # Deduplicate within the same image
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for c in codes:
        if c["data"] not in seen:
            seen.add(c["data"])
            unique.append(c)

    return {
        "status": "success",
        "codes": unique,
        "file": str(p),
        "count": len(unique),
    }


def scan_qr_batch(file_paths: list[str]) -> dict[str, Any]:
    """Batch-scan multiple image files for barcodes / QR codes.

    Returns summary dict with per-file results, aggregated unique codes,
    and deduplication info.
    """
    results: list[dict] = []
    errors: list[dict] = []

    # Track unique codes across all files
    code_to_files: dict[str, list[str]] = {}
    code_to_types: dict[str, str] = {}

    for fp in file_paths:
        r = scan_qr_from_file(fp)
        results.append(r)
        if r["status"] == "success":
            fname = Path(fp).name
            for c in r.get("codes", []):
                data = c["data"]
                if data not in code_to_files:
                    code_to_files[data] = []
                    code_to_types[data] = c["type"]
                code_to_files[data].append(fname)
        elif r["status"] == "error":
            errors.append(r)
        # "no_code_found" is fine — just skip

    # Build deduplicated unique codes
    unique_codes: list[dict] = []
    for data, files in code_to_files.items():
        unique_codes.append({
            "data": data,
            "type": code_to_types.get(data, "unknown"),
            "found_in_files": files,
            "occurrence_count": len(files),
        })

    return {
        "status": "success" if not errors else "partial",
        "total_files": len(file_paths),
        "files_with_codes": sum(1 for r in results if r["status"] == "success"),
        "files_without_codes": sum(1 for r in results if r["status"] == "no_code_found"),
        "files_with_errors": len(errors),
        "unique_codes": unique_codes,
        "total_unique_codes": len(unique_codes),
        "per_file": results,
        "errors": errors if errors else None,
    }


def _convert_heic_to_jpg(heic_path: str) -> str | None:
    """Convert HEIC to JPEG using macOS sips. Returns jpg path or None."""
    p = Path(heic_path)
    if p.suffix.lower() not in (".heic", ".heif"):
        return None
    jpg_path = p.with_suffix(".jpg")
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(p), "--out", str(jpg_path)],
            capture_output=True, timeout=30, check=True,
        )
        return str(jpg_path)
    except Exception as e:
        logger.warning("HEIC conversion failed for %s: %s", heic_path, e)
        return None


# ──────────────────────────── QR Lookup ────────────────────────────

def lookup_qr_code(qr_code: str) -> dict[str, Any]:
    """Look up a single QR code's DAO record via the GAS web app (read-only).

    Uses the same backend as `dao_client`'s `lookup_qr_code` module.

    Returns:
        {"status": "success", "qr_code": "...", "currency": "...", ...}
        or {"status": "error", "message": "..."}
    """
    try:
        from truesight_dao_client.modules.lookup_qr_code import lookup
        data = lookup(qr_code)
        if data.get("status") == "success":
            _cache_qr_result(qr_code, data)
            return data
        return {
            "status": "error",
            "message": data.get("message", "Unknown error"),
            "raw": data,
        }
    except ImportError:
        # Fallback: run the module as a subprocess
        result = _lookup_qr_via_cli(qr_code)
        if result.get("status") == "success":
            _cache_qr_result(qr_code, result)
        return result
    except Exception as e:
        logger.error("QR lookup failed for %s: %s", qr_code, e)
        return {"status": "error", "message": str(e), "qr_code": qr_code}


def _lookup_qr_via_cli(qr_code: str) -> dict[str, Any]:
    """Fallback lookup via dao_client CLI subprocess."""
    workspace_root = Path(__file__).resolve().parents[3]
    dao_client_dir = workspace_root / "dao_client"

    cmd = [
        sys.executable, "-m", "truesight_dao_client.modules.lookup_qr_code",
        "--qr", qr_code, "--json",
    ]
    env = {**os.environ}
    env["PYTHONPATH"] = str(dao_client_dir)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            env=env, cwd=str(dao_client_dir),
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        return {"status": "error", "message": result.stderr or result.stdout, "qr_code": qr_code}
    except Exception as e:
        return {"status": "error", "message": str(e), "qr_code": qr_code}


def lookup_qr_batch(qr_codes: list[str]) -> dict[str, Any]:
    """Look up multiple QR codes and return a summary."""
    results: list[dict] = []
    found: list[dict] = []
    missing: list[str] = []
    lookup_errors: list[dict] = []

    for code in qr_codes:
        r = lookup_qr_code(code)
        results.append(r)
        if r.get("status") == "success":
            found.append(r)
            _cache_qr_result(code, r)
        elif r.get("status") == "error":
            lookup_errors.append(r)
        else:
            missing.append(code)

    return {
        "status": "success" if not lookup_errors else "partial",
        "total": len(qr_codes),
        "found": len(found),
        "missing": len(missing),
        "errors": len(lookup_errors),
        "records": found,
        "missing_codes": missing if missing else None,
        "error_details": lookup_errors if lookup_errors else None,
    }
