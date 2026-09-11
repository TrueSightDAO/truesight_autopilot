#!/usr/bin/env python3
"""Convert a phone image (HEIC/HEIF/JPEG) into a web-ready JPEG with a
NORMALIZED orientation tag.

Why this exists (bug, 2026-09-11)
---------------------------------
The ad-hoc ``heif-convert -> 1600px -> q80`` step that built the sunmint
boundary photos BAKES the EXIF rotation into the output pixels but LEAVES the
original ``Orientation`` tag (3/6/8) in the output file. A browser honours that
now-stale tag and rotates a SECOND time, so the photo renders sideways (18
files across CR-PA-P2 / N-06-37 / N-06-66 / PL-002 were affected).

This script is the canonical replacement. It applies the orientation exactly
once (``ImageOps.exif_transpose``, which also resets the tag to normal) and
writes the JPEG WITHOUT an exif block, so pixels and tag can never disagree.

Usage
-----
    python3 scripts/prepare_web_image.py <input> [-o out.jpg] \
        [--max-dim 1600] [-q 82]

    # audit only: report any file whose orientation tag is non-normal
    python3 scripts/prepare_web_image.py --check <files...>

Exit codes: 0 ok, 1 conversion error, 2 usage error (--check: 1 if any stale).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WEB_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
DEFAULT_MAX_DIM = 1600
DEFAULT_QUALITY = 82


def read_orientation(image_path: str | Path) -> int:
    """Return the EXIF Orientation tag (int, 1 = normal) or 1 when absent.

    Opens with pillow_heif's opener registered so HEIC/HEIF originals are read
    correctly. Never raises on a missing tag -- absence means normal.
    """
    from PIL import Image

    _register_heif()
    with Image.open(image_path) as im:
        tag = im.getexif().get(274)
    try:
        return int(tag) if tag is not None else 1
    except (TypeError, ValueError):
        return 1


def _register_heif() -> bool:
    """Register pillow_heif's Pillow opener if the package is importable."""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def normalize_orientation(image):
    """Bake the EXIF orientation into pixels and reset the tag to normal.

    ``ImageOps.exif_transpose`` both rotates the pixels to their display
    orientation and drops the orientation tag, so a subsequent save cannot
    leave a stale tag behind. Returns a new image; the input is left as-is.
    """
    from PIL import ImageOps

    return ImageOps.exif_transpose(image)


def to_web_jpeg(
    image_path: str | Path,
    out_path: str | Path,
    max_dim: int = DEFAULT_MAX_DIM,
    quality: int = DEFAULT_QUALITY,
) -> Path:
    """Convert ``image_path`` to a web-ready JPEG at ``out_path``.

    - HEIC/HEIF (and any Pillow-supported source) is decoded via pillow_heif.
    - The orientation is baked in once and the tag is NOT re-emitted.
    - The longest side is downscaled to at most ``max_dim`` (never upscaled).
    - Output is progressive JPEG, metadata-free (no stale orientation, no GPS).
    """
    from PIL import Image

    src = Path(image_path)
    out = Path(out_path)
    if src.suffix.lower() not in WEB_IMAGE_EXTENSIONS:
        raise ValueError(f"unsupported image type: {src.suffix}")
    if src.suffix.lower() in {".heic", ".heif"} and not _register_heif():
        raise RuntimeError("pillow_heif is required to decode HEIC/HEIF")

    with Image.open(src) as im:
        im = normalize_orientation(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        longest = max(w, h)
        if max_dim and longest > max_dim:
            scale = max_dim / float(longest)
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            im = im.resize(new_size, Image.LANCZOS)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Intentionally no `exif=` kwarg: the stale orientation tag must not
        # survive into the derivative (that omission IS the bug fix).
        im.save(out, format="JPEG", quality=quality, progressive=True, optimize=True)
    return out


def check_files(paths) -> int:
    """Print the orientation of each file; return count of non-normal files."""
    stale = 0
    for p in paths:
        try:
            tag = read_orientation(p)
        except Exception as e:  # pragma: no cover - defensive
            print(f"ERROR {p}: {e}")
            stale += 1
            continue
        flag = "OK " if tag == 1 else "STALE"
        if tag != 1:
            stale += 1
        print(f"{flag} orientation={tag} {p}")
    return stale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", nargs="+", help="input image path(s)")
    ap.add_argument("-o", "--out", help="output path (single input only)")
    ap.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)
    ap.add_argument("-q", "--quality", type=int, default=DEFAULT_QUALITY)
    ap.add_argument(
        "--check",
        action="store_true",
        help="only report orientation tags, do not convert",
    )
    args = ap.parse_args(argv)

    if args.check:
        return 1 if check_files(args.input) else 0

    if len(args.input) > 1 and args.out:
        print("--out accepts a single input", file=sys.stderr)
        return 2

    rc = 0
    for src in args.input:
        dst = args.out or str(Path(src).with_suffix(".jpg"))
        try:
            to_web_jpeg(src, dst, max_dim=args.max_dim, quality=args.quality)
            print(f"wrote {dst}")
        except Exception as e:
            print(f"ERROR {src}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
