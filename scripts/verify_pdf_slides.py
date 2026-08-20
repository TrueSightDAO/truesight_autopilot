#!/usr/bin/env python3
"""Verify a generated PDF slide deck has NO overlapping elements.

Catches the four overlap classes that have caused real defects in Sophia's
slide decks (Agroverse x CEPOTX deck, Aug 2026):
  1. text vs text            (word boxes intersecting beyond a small bbox-kiss)
  2. text vs image           (words overlapping embedded CONTENT images --
                             full-bleed background photos are exempt by design)
  3. text vs drawn panels    (words spilling over the edge of cream/green
                             content panels -- the bug that bit us: panels
                             starting too high and painting over titles)
  4. panels vs header/footer (content panels intruding into the saffron
                             header band or the footer area)

Usage:
    python3 scripts/verify_pdf_slides.py path/to/deck.pdf [--header-px 110]
Exit code 0 = clean, 1 = problems found (with a per-page report).

Companion: agentic_ai_context/SLIDE_DECK_STANDARD.md (mandatory QA step).
Requires: PyMuPDF (fitz).
"""

from __future__ import annotations

import argparse
import sys

try:
    import fitz  # PyMuPDF
except ImportError as e:  # pragma: no cover
    sys.exit(f"PyMuPDF required: pip install pymupdf ({e})")


def _inter(a: tuple, b: tuple) -> tuple[float, float, float]:
    """Return (x_overlap, y_overlap, area) of two (x0,y0,x1,y1) rects."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0, 0.0
    ox, oy = x1 - x0, y1 - y0
    return ox, oy, ox * oy


def verify_pdf(path: str, header_px: int = 110) -> list[str]:
    """Return a list of human-readable problems; empty list == clean."""
    doc = fitz.open(path)
    problems: list[str] = []
    pw, ph = doc[0].rect.width, doc[0].rect.height
    header = fitz.Rect(0, 0, pw, header_px)

    for pi, page in enumerate(doc, start=1):
        words = page.get_text("words")
        # Images: anything covering >= 90% of the page is a full-bleed
        # background (standard format) -- text sits on it BY DESIGN.
        images = [fitz.Rect(im["bbox"]) for im in page.get_image_info()]
        bg_images = [r for r in images if r.get_area() >= 0.9 * pw * ph]
        content_images = [r for r in images if r not in bg_images]
        # Drawn shapes: content panels are big filled rects (w>100,h>80)
        # that are NOT the full-width header band and NOT a full-page overlay.
        drawings = [
            fitz.Rect(d["rect"])
            for d in page.get_drawings()
            if d["rect"].width > 0 and d["rect"].height > 0
        ]
        panels = [
            r
            for r in drawings
            if r.width > 100
            and r.height > 80
            and not (r.width > pw - 20 and r.height == header_px and r.y0 < 5)
            and r.get_area() < 0.9 * pw * ph
        ]

        # 1) text vs text -- require a real collision in BOTH axes (>6px),
        #    so stacked title lines (3px bbox kiss) don't false-positive.
        for a in range(len(words)):
            for b in range(a + 1, len(words)):
                wa, wb = words[a], words[b]
                if wa[5] == wb[5] and wa[6] == wb[6]:
                    continue  # same text block/line
                ox, oy, _ = _inter(wa[:4], wb[:4])
                if ox > 6.0 and oy > 6.0:
                    problems.append(f"p{pi} text×text: '{wa[4]}' × '{wb[4]}'")

        # 2) text vs CONTENT image (background photos exempt)
        for w in words:
            wr = fitz.Rect(w[:4])
            for ir in content_images:
                ox, oy, _ = _inter(wr, ir)
                if ox > 2.0 and oy > 2.0:
                    problems.append(f"p{pi} text×image: '{w[4]}' over image")

        # 3) text spilling over panel edges
        for w in words:
            wr = fitz.Rect(w[:4])
            for pr in panels:
                ox, oy, _ = _inter(wr, pr)
                if (
                    ox > 2.0
                    and oy > 2.0
                    and pr.contains(fitz.Point(wr.x0 + 2, wr.y0 + 2))
                ):
                    if not (
                        wr.x0 >= pr.x0 - 0.5
                        and wr.x1 <= pr.x1 + 0.5
                        and wr.y0 >= pr.y0 - 0.5
                        and wr.y1 <= pr.y1 + 0.5
                    ):
                        problems.append(f"p{pi} text×panel: '{w[4]}' over panel edge")

        # 4) content panels intruding into header/footer bands
        for pr in panels:
            ox, oy, _ = _inter(pr, header)
            if ox > 1.0 and oy > 1.0:
                problems.append(
                    f"p{pi} panel×header: panel top y={pr.y0:.0f} enters header band"
                )
        for pr in panels:
            if pr.y1 > ph - 30:
                problems.append(
                    f"p{pi} panel×footer: panel bottom y={pr.y1:.0f} past footer"
                )

    doc.close()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", help="path to the PDF deck to verify")
    ap.add_argument(
        "--header-px",
        type=int,
        default=110,
        help="header band height in points (default 110)",
    )
    args = ap.parse_args()

    problems = verify_pdf(args.pdf, header_px=args.header_px)
    if not problems:
        print(f"OK  {args.pdf} — 0 overlap problems")
        return 0
    print(f"FAIL {args.pdf} — {len(problems)} overlap problem(s):")
    for p in problems:
        print("  -", p)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
