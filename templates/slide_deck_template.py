#!/usr/bin/env python3
"""TrueSight DAO slide deck renderer — Saffron Monk 16:9 format.

Companion spec: agentic_ai_context/SLIDE_DECK_STANDARD.md (mandatory for all
Sophia / LLM-generated decks). This is the generalized version of the renderer
behind TrueSightDAO_Talk_Deck v8.

Usage:
    python3 slide_deck_template.py
Output:
    <OUT>/TrueSightDAO_<SLUG>.pdf

Edit CONFIG and SLIDES below. Photos must exist locally (curate from
agroverse_shop_beta/assets, sunmint, lineage-assets per the standard).
"""

from PIL import Image, ImageOps
import os

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
OUT = "/tmp/slides_img"
BG = OUT + "/bg"
os.makedirs(BG, exist_ok=True)

SLUG = "talk_deck"
BAND = "TRUE\u00b7SIGHT DAO \u00d7 AGROVERSE  \u00b7  BUILDING WITH AI IN DAOs, GOVERNANCE & COLLECTIVE ORGANIZATIONS"
FOOTER = "truesight.me  \u00b7  agroverse.shop  \u00b7  TrueSight DAO"
OUTFILE = f"{OUT}/TrueSightDAO_{SLUG}.pdf"

# (R, G, B) floats — Saffron Monk palette, do not change without approval
SAFFRON = (0.847, 0.463, 0.098)
CREAM = (0.984, 0.953, 0.894)
DARK = (0.16, 0.13, 0.09)
GREY = (0.42, 0.38, 0.33)
BLUE = (0.10, 0.37, 0.71)

# ---------------------------------------------------------------------------
# Image prep
# ---------------------------------------------------------------------------
def crop16x9(src, dst, w=1920, h=1080):
    im = Image.open(src)
    im = ImageOps.exif_transpose(im)  # honor EXIF orientation (phone photos)
    if im.mode != "RGB":
        im = im.convert("RGB")
    W, H = im.size
    tgt = w / h
    cur = W / H
    if cur > tgt:
        nw = int(H * tgt)
        x0 = (W - nw) // 2
        im = im.crop((x0, 0, x0 + nw, H))
    else:
        nh = int(W / tgt)
        y0 = (H - nh) // 2
        im = im.crop((0, y0, W, y0 + nh))
    im = im.resize((w, h), Image.LANCZOS)
    im.save(dst, "JPEG", quality=82)
    return dst

# ---------------------------------------------------------------------------
# PDF primitives
# ---------------------------------------------------------------------------
from reportlab.pdfgen import canvas as rc
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

W, H = 960, 540


def wrap(text, font, size, maxw):
    lines, cur = [], ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if stringWidth(t, font, size) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def draw_wrapped(c, x, y, text, font, size, maxw, leading, color=DARK,
                 align="left", url=None, underline=False):
    lines = wrap(text, font, size, maxw)
    for i, ln in enumerate(lines):
        yy = y - i * leading
        c.setFillColorRGB(*color)
        c.setFont(font, size)
        if align == "left":
            c.drawString(x, yy, ln)
            lx, lw = x, stringWidth(ln, font, size)
        elif align == "center":
            c.drawCentredString(x, yy, ln)
            lw = stringWidth(ln, font, size)
            lx = x - lw / 2
        elif align == "right":
            c.drawRightString(x, yy, ln)
            lw = stringWidth(ln, font, size)
            lx = x - lw
        if underline:
            c.setStrokeColorRGB(*color)
            c.setLineWidth(0.7)
            c.line(lx, yy - 1.5, lx + lw, yy - 1.5)
        if url:
            c.linkURL(url, (lx, yy - size * 0.25, lx + lw, yy + size * 0.85), relative=1)
    return y - len(lines) * leading


def bullets(c, x, y, items, size=12.5, lead=21, maxw=540, gap=8):
    """items: list of str or (text, url). Clickable when url given."""
    yy = y
    for it in items:
        if isinstance(it, tuple):
            text, url = it
        else:
            text, url = it, None
        color = BLUE if url else DARK
        c.setFillColorRGB(*color)
        c.setFont("Helvetica", size)
        c.drawString(x, yy, "\u2022")
        lines = wrap(text, "Helvetica", size, maxw - 18)
        for ln in lines:
            c.setFillColorRGB(*color)
            c.setFont("Helvetica", size)
            c.drawString(x + 18, yy, ln)
            if url:
                c.setStrokeColorRGB(*color)
                c.setLineWidth(0.7)
                c.line(x + 18, yy - 1.5, x + 18 + stringWidth(ln, "Helvetica", size), yy - 1.5)
                c.linkURL(url, (x + 18, yy - size * 0.25,
                                x + 18 + stringWidth(ln, "Helvetica", size), yy + size * 0.85), relative=1)
            yy -= lead
        yy -= gap
    return yy


def bg(c, path):
    c.drawImage(ImageReader(path), 0, 0, W, H)


def dim(c, a=0.52):
    c.saveState()
    c.setFillColorRGB(0, 0, 0)
    c.setFillAlpha(a)
    c.rect(0, 0, W, H, stroke=0, fill=1)
    c.restoreState()


def band(c, page, total, band_text=BAND):
    c.saveState()
    c.setFillColorRGB(*SAFFRON)
    c.rect(0, H - 34, W, 34, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(W / 2, H - 24, band_text)
    c.drawRightString(W - 16, H - 24, f"{page} / {total}")
    c.restoreState()


def footer(c, footer_text=FOOTER):
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.setFillAlpha(0.75)
    c.setFont("Helvetica", 8)
    c.drawRightString(W - 24, 12, footer_text)
    c.restoreState()


def panel(c, x, y, w, h, a=0.88):
    c.saveState()
    c.setFillColorRGB(*CREAM)
    c.setFillAlpha(a)
    c.roundRect(x, y, w, h, 8, stroke=0, fill=1)
    c.restoreState()


# ---------------------------------------------------------------------------
# SLIDES — define content here per slide
# ---------------------------------------------------------------------------
# Each slide: dict with keys bg_img, title, subtitle, bullets (list of
# str | (text,url)), optional qr_img + qr_url + qr_pos, panel coords.
SLIDES = [
    {
        "bg_img": f"{BG}/hook.jpg",
        "panel": (40, 120, 620, 300),
        "title_lines": [
            ("10,000 hectares of Amazon rainforest \u2014", "Helvetica-Bold", 27),
            ("one bag of ceremonial cacao at a time.", "Helvetica-Bold", 27),
        ],
        "subtitle": "TrueSight DAO \u00d7 Agroverse \u2014 an AI-run DAO, with a human governor in the loop",
        "teaser": "Five systems run the DAO: supply chain \u00b7 signed audit trail \u00b7 Email360 outreach \u00b7 community \u00b7 daily Beer Hall digest",
        "bullets": [],
    },
]

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
c = rc.Canvas(OUTFILE, pagesize=(W, H))
TOTAL = len(SLIDES)


def render_slide(c, s, page, total):
    bg(c, s["bg_img"])
    dim(c)
    band(c, page, total, s.get("band", BAND))
    px, py, pw, ph = s["panel"]
    panel(c, px, py, pw, ph)
    x, top = px + 24, py + ph - 34
    for t, font, size in s.get("title_lines", []):
        top = draw_wrapped(c, x, top, t, font, size, pw - 48, size * 1.33, color=DARK)
        top -= 6
    if s.get("subtitle"):
        top = draw_wrapped(c, x, top, s["subtitle"], "Helvetica-Oblique", 12, pw - 48, 18, color=GREY)
        top -= 8
    if s.get("bullets"):
        top = bullets(c, x, top, s["bullets"], maxw=pw - 48)
        top -= 8
    if s.get("teaser"):
        draw_wrapped(c, x, top, s["teaser"], "Helvetica", 12.5, pw - 48, 19, color=DARK)
    if s.get("qr_img"):
        qr = Image.open(s["qr_img"]).convert("RGB")
        qw, qh = s.get("qr_size", (200, 155))
        qx, qy = s.get("qr_pos", (W - 320, py + 90))
        c.drawImage(ImageReader(qr.resize(qw, Image.LANCZOS)), qx, qy, qw, qh)
        if s.get("qr_url"):
            c.linkURL(s["qr_url"], (qx, qy, qx + qw, qy + qh), relative=1)
    footer(c, s.get("footer", FOOTER))
    c.showPage()


for i, s in enumerate(SLIDES, 1):
    render_slide(c, s, i, TOTAL)
c.save()
print(f"DONE size={os.path.getsize(OUTFILE)}")
