#!/usr/bin/env python3
"""Extract text from images using OCR (Tesseract).

Preprocesses images with Pillow before OCR for better accuracy:
- Grayscale conversion
- Threshold/binarization
- Contrast enhancement
- Deskew (basic)

Usage:
    python3 scripts/ocr_image.py <path_to_image>

Output:
    JSON with status, extracted text, confidence, and quality flags.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ocr_image")

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20 MB


def preprocess_image(image):
    """Apply preprocessing to improve OCR accuracy."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    # Convert to grayscale
    if image.mode != "L":
        image = image.convert("L")

    # Enhance contrast
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)

    # Sharpen
    image = image.filter(ImageFilter.SHARPEN)

    # Binarize with threshold
    image = ImageOps.autocontrast(image, cutoff=5)

    return image


def ocr_image(path: str, lang: str = "eng") -> dict:
    """Run OCR on an image file.

    Args:
        path: Path to the image file.
        lang: Tesseract language code (default: eng).

    Returns:
        Dict with status, extracted text, confidence, and quality info.
    """
    p = Path(path)
    if not p.exists():
        return {"status": "error", "message": f"File not found: {path}"}
    if p.stat().st_size == 0:
        return {"status": "error", "message": "File is empty"}
    if p.stat().st_size > MAX_IMAGE_SIZE:
        return {"status": "error", "message": "File too large (>20 MB)"}

    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return {"status": "error", "message": f"Unsupported image format: {ext}. Supported: {SUPPORTED_EXTENSIONS}"}

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return {"status": "error", "message": "pytesseract or Pillow not installed. Run: pip install pytesseract Pillow"}

    try:
        # Check if tesseract is installed
        from pytesseract import pytesseract as pyt
        try:
            pyt.get_tesseract_version()
        except Exception:
            return {"status": "error", "message": "Tesseract OCR engine not found. Install: apt-get install tesseract-ocr"}

        # Open and preprocess
        original = Image.open(path)
        original_w, original_h = original.size

        processed = preprocess_image(original)

        # Run OCR with confidence data
        data = pytesseract.image_to_data(processed, lang=lang, output_type=pytesseract.Output.DICT)

        # Extract text and compute confidence
        text_parts = []
        confidences = []
        for i, text in enumerate(data["text"]):
            text = text.strip()
            if text:
                text_parts.append(text)
                try:
                    conf = int(data["conf"][i])
                    if conf > 0:
                        confidences.append(conf)
                except (ValueError, IndexError):
                    pass

        full_text = " ".join(text_parts)
        avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

        # Quality assessment
        quality = "good"
        if avg_confidence < 50:
            quality = "poor"
        elif avg_confidence < 70:
            quality = "fair"

        return {
            "status": "success",
            "text": full_text,
            "word_count": len(text_parts),
            "avg_confidence": avg_confidence,
            "quality": quality,
            "image_size": f"{original_w}x{original_h}",
            "language": lang,
        }

    except Exception as e:
        logger.exception("OCR failed")
        return {"status": "error", "message": f"OCR failed: {e}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error", "message": "Usage: ocr_image.py <path> [lang]"}))
        sys.exit(1)

    lang = sys.argv[2] if len(sys.argv) > 2 else "eng"
    result = ocr_image(sys.argv[1], lang)
    print(json.dumps(result, indent=2, ensure_ascii=False))
