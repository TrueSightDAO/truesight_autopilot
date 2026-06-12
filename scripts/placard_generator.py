#!/usr/bin/env python3
"""
Placard Generator — CLI tool for generating branded landscape placards
with QR codes for event table displays.

Usage:
    python3 scripts/placard_generator.py \\
        --qr-code "SFTF_FR_20260612_2" \\
        --event "SF Tech Fest 2026" \\
        --collection "Friends of the Rainforest" \\
        --origin "Brazilian Amazon Rainforest" \\
        --url "agroverse.shop/friends-of-the-rainforest"

Output:
    - PNG saved locally
    - Uploaded to TrueSightDAO/lineage-assets/pngs/{QR_CODE}_placard.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import qrcode
from qrcode.constants import ERROR_CORRECT_H

# ── Constants ──────────────────────────────────────────────────────────────
WIDTH = 1650
HEIGHT = 1275
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LOGO_PATH = "/opt/truesight_autopilot/tokenomics/agroverse_qr_code_web_service/logos/agroverse_logo.jpeg"
TARGET_REPO = "TrueSightDAO/lineage-assets"
SAFFRON = (230, 126, 34)
GREEN = (46, 125, 50)
CREAM = (250, 248, 242)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONT_PATH_BOLD if bold and os.path.exists(FONT_PATH_BOLD) else FONT_PATH
    if os.path.exists(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _draw_wrapped_text(draw, text, x, y, max_width, font, fill):
    """Word-wrap text and draw it, returning final y position."""
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = current + (" " if current else "") + word
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    lh = draw.textbbox((0, 0), "Ay", font=font)[3] - draw.textbbox((0, 0), "A", font=font)[1] + 8
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += lh
    return y


def generate_placard(
    qr_code: str,
    event_name: str,
    collection: str,
    origin: str,
    landing_url: str,
    mission: str,
    output_path: str | Path | None = None,
) -> Path:
    """Generate a branded landscape placard and return the output path."""
    f_title = _load_font(68, bold=True)
    f_sub = _load_font(38)
    f_body = _load_font(30, bold=True)
    f_small = _load_font(26)
    f_tiny = _load_font(22)

    placard = Image.new("RGBA", (WIDTH, HEIGHT), CREAM)
    draw = ImageDraw.Draw(placard)

    # Top banner
    draw.rectangle([(0, 0), (WIDTH, 10)], fill=(*SAFFRON, 255))
    draw.rectangle([(0, 10), (WIDTH, 105)], fill=(240, 147, 43, 255))
    draw.rectangle([(0, 105), (WIDTH, 115)], fill=(200, 100, 20, 255))
    tw = draw.textbbox((0, 0), event_name, font=f_title)[2]
    draw.text(((WIDTH - tw) // 2, 22), event_name, fill="white", font=f_title)

    # QR code
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(f"https://edgar.truesight.me/agroverse/qr-code-check?qr_code={qr_code}")
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    QR_SIZE = qr_img.size[0]

    if os.path.exists(LOGO_PATH):
        logo = Image.open(LOGO_PATH).convert("RGBA")
        max_logo = int(QR_SIZE * 0.22)
        logo.thumbnail((max_logo, max_logo), Image.Resampling.LANCZOS)
        pos = ((QR_SIZE - logo.size[0]) // 2, (QR_SIZE - logo.size[1]) // 2)
        qr_img.paste(logo, pos, logo)

    qr_x, qr_y = 50, 155
    shadow = Image.new("RGBA", (QR_SIZE + 16, QR_SIZE + 16), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [(8, 8), (QR_SIZE + 8, QR_SIZE + 8)], radius=20, fill=(0, 0, 0, 35)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=10))
    placard.paste(shadow, (qr_x - 8, qr_y - 8), shadow)
    draw.rounded_rectangle(
        [(qr_x - 12, qr_y - 12), (qr_x + QR_SIZE + 12, qr_y + QR_SIZE + 12)],
        radius=14, fill=(255, 255, 255), outline=(220, 220, 220), width=2,
    )
    placard.paste(qr_img, (qr_x, qr_y), qr_img)

    scan_text = "Scan to support the Amazon Rainforest"
    stw = draw.textbbox((0, 0), scan_text, font=f_small)[2]
    draw.text((qr_x + (QR_SIZE - stw) // 2, qr_y + QR_SIZE + 18), scan_text, fill=(120, 120, 120), font=f_small)

    # Right side
    right_x = qr_x + QR_SIZE + 45
    right_w = WIDTH - right_x - 40

    draw.text((right_x, 160), collection, fill=SAFFRON, font=f_sub)
    draw.line([(right_x, 210), (right_x + 250, 210)], fill=SAFFRON, width=3)

    # Info box
    box_y, box_h, box_pad = 245, 480, 25
    draw.rounded_rectangle(
        [(right_x, box_y), (right_x + right_w, box_y + box_h)],
        radius=16, fill=(255, 255, 255), outline=SAFFRON, width=2,
    )
    draw.text((right_x + box_pad, box_y + 18), "About This Cacao", fill=SAFFRON, font=f_body)
    draw.line(
        [(right_x + box_pad, box_y + 55), (right_x + right_w - box_pad, box_y + 55)],
        fill=(240, 240, 240), width=2,
    )

    val_offset, max_val_w = 185, right_w - 185 - box_pad - 5
    items = [
        ("Origin", origin),
        ("Collection", collection),
        ("Batch", qr_code),
    ]
    ry = box_y + 80
    for label, val in items:
        draw.text((right_x + box_pad, ry), label, fill=(140, 140, 140), font=f_small)
        dv = val
        while draw.textbbox((0, 0), dv, font=f_small)[2] > max_val_w and len(dv) > 5:
            dv = dv[:-1]
        if dv != val:
            dv = dv[:-3] + "..."
        draw.text((right_x + box_pad + val_offset, ry), dv, fill=(40, 40, 40), font=f_small)
        ry += 52

    # Web link
    ry += 5
    draw.text((right_x + box_pad, ry), "Web", fill=(140, 140, 140), font=f_small)
    dv = landing_url
    while draw.textbbox((0, 0), dv, font=f_small)[2] > max_val_w and len(dv) > 10:
        dv = dv[:-1]
    if dv != landing_url:
        dv = dv[:-3] + "..."
    draw.text((right_x + box_pad + val_offset, ry), dv, fill=(30, 120, 200), font=f_small)

    # Mission text
    _draw_wrapped_text(draw, mission, right_x, box_y + box_h + 25, right_w, f_small, (100, 100, 100))

    # Bottom band
    by = HEIGHT - 85
    draw.rectangle([(0, by), (WIDTH, HEIGHT)], fill=(*GREEN, 245))
    footer = "TrueSight DAO  |  truesight.me"
    fw = draw.textbbox((0, 0), footer, font=f_tiny)[2]
    draw.text(((WIDTH - fw) // 2, by + 18), footer, fill="white", font=f_tiny)
    fw2 = draw.textbbox((0, 0), "10,000 Hectares of Amazon Rainforest", font=f_tiny)[2]
    draw.text(((WIDTH - fw2) // 2, by + 48), "10,000 Hectares of Amazon Rainforest", fill=(200, 230, 200), font=f_tiny)

    # Border
    draw.rounded_rectangle([(8, 8), (WIDTH - 8, HEIGHT - 8)], radius=20, outline=SAFFRON, width=4)
    for cx, cy in [(8, 8), (WIDTH - 8, 8), (8, HEIGHT - 8), (WIDTH - 8, HEIGHT - 8)]:
        draw.rounded_rectangle([(cx, cy), (cx + 40, cy + 40)], radius=10, fill=SAFFRON)

    if output_path is None:
        output_path = Path(f"/tmp/{qr_code}_placard.png")
    placard.save(str(output_path))
    return Path(output_path)


def upload_to_github(file_path: Path, qr_code: str, github_token: str | None = None) -> str:
    """Upload placard PNG to lineage-assets and return the raw URL."""
    if github_token is None:
        github_token = os.popen(
            'grep "^TRUESIGHT_DAO_AUTOPILOT=" /opt/truesight_autopilot/.env | cut -d= -f2'
        ).read().strip()

    with open(file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    target_path = f"pngs/{qr_code}_placard.png"
    api_url = f"https://api.github.com/repos/{TARGET_REPO}/contents/{target_path}"

    req = urllib.request.Request(api_url, headers={"Authorization": f"token {github_token}"})
    sha = None
    try:
        with urllib.request.urlopen(req) as r:
            sha = json.loads(r.read()).get("sha")
    except urllib.error.HTTPError:
        pass

    payload = {"message": f"Update placard: {qr_code}", "content": content}
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"token {github_token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())

    raw_url = f"https://raw.githubusercontent.com/{TARGET_REPO}/main/{target_path}"
    print(f"Uploaded: {raw_url}")
    return raw_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate branded landscape placard with QR code for event table displays."
    )
    parser.add_argument("--qr-code", required=True, help="QR code identifier (e.g. SFTF_FR_20260612_2)")
    parser.add_argument("--event", required=True, help="Event display name (e.g. SF Tech Fest 2026)")
    parser.add_argument("--collection", required=True, help="Collection name (e.g. Friends of the Rainforest)")
    parser.add_argument("--origin", default="Brazilian Amazon Rainforest", help="Origin text")
    parser.add_argument("--url", required=True, help="Short landing URL for display")
    parser.add_argument("--mission", default="Every purchase helps restore 10,000 hectares of Amazon Rainforest through regenerative agroforestry with local farming communities.", help="Mission statement")
    parser.add_argument("--output", default=None, help="Output file path (default: /tmp/{qr_code}_placard.png)")
    parser.add_argument("--no-upload", action="store_true", help="Skip GitHub upload")
    parser.add_argument("--dry-run", action="store_true", help="Generate PNG but don't upload")
    args = parser.parse_args(argv)

    output = args.output or f"/tmp/{args.qr_code}_placard.png"
    path = generate_placard(
        qr_code=args.qr_code,
        event_name=args.event,
        collection=args.collection,
        origin=args.origin,
        landing_url=args.url,
        mission=args.mission,
        output_path=output,
    )
    print(f"Placard saved: {path} ({path.stat().st_size} bytes)")

    if not args.no_upload and not args.dry_run:
        upload_to_github(path, args.qr_code)
        print(f"View: https://raw.githubusercontent.com/{TARGET_REPO}/main/pngs/{args.qr_code}_placard.png")

    return 0


if __name__ == "__main__":
    sys.exit(main())
