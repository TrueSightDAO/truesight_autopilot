"""Generate a brand-styled PDF from text/markdown for the autopilot agent.

Uses ``reportlab`` (pure-Python, no system deps) to render content into a
multi-page PDF following the **TrueSight DAO PDF house style** (Saffron Monk):
see ``agentic_ai_context/PDF_STYLE_CONVENTION.md``. The output is returned as
base64-encoded bytes so the agent can feed it to ``upload_file_to_github`` or
write it locally.

House style applied here:
- Saffron header band (#C98A2D) on every page with the document title.
- Helvetica family for text (the dependency-free stand-in for Helvetica Neue —
  the box has no Helvetica-Neue TTF; built-in Helvetica matches it closely),
  Courier for code/mono.
- Cacao-brown headings, #222 body, muted-gray footer with page numbers.
- **Markdown tables render as real tables** (gray header + zebra rows) — never
  as raw ``| pipe |`` text.
- **CJK / Chinese text**: if the content contains non-ASCII characters (e.g.
  Chinese, Japanese, Korean), the DroidSansFallbackFull.ttf font is registered
  and used instead of Helvetica, ensuring CJK glyphs render correctly.

Markdown subset: ``#``/``##``/``###`` headings, paragraphs, ``-``/``*`` bullets,
``**bold`` / ``*italic*``, and pipe tables (``| a | b |`` + ``|---|---|``).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tempfile
from typing import Any

logger = logging.getLogger("autopilot.tools.pdf_tools")

_MAX_BYTES_RETURN = 256 * 1024  # cap on base64 returned to the model
_HARD_PAGE_LIMIT = 200  # safety net for runaway loops

# ── Brand palette (Saffron Monk) — see PDF_STYLE_CONVENTION.md ──────────────
_SAFFRON = "#C98A2D"  # primary accent / header band
_CLAY = "#8A5A1D"  # secondary accent / links
_CACAO_DARK = "#3D2B1F"  # titles / strong headings
_CACAO_MID = "#5A4632"  # subheads
_BODY = "#222222"  # body text
_MUTED = "#888888"  # captions / footer
_RULE = "#DDDDDD"  # table header fill / separators
_ZEBRA = "#FBF7EF"  # light cream zebra row
_WHITE = "#FFFFFF"

_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_ITALIC = "Helvetica-Oblique"
_FONT_MONO = "Courier"

# ── CJK font support ──────────────────────────────────────────────────────
_DROID_FONT_PATH = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
_CJK_FONT_NAME = "DroidSansFallbackFull"
_cjk_registered = False  # module-level flag: register once


def _ensure_cjk_font() -> str | None:
    """Register DroidSansFallbackFull.ttf with reportlab if available.

    Returns the font name (``_CJK_FONT_NAME``) on success, or ``None`` if the
    font file is missing / registration fails.
    """
    global _cjk_registered
    if _cjk_registered:
        return _CJK_FONT_NAME
    if not os.path.isfile(_DROID_FONT_PATH):
        logger.warning("CJK font not found at %s — falling back to Helvetica", _DROID_FONT_PATH)
        return None
    try:
        from reportlab.pdfbase import pdfmetrics  # type: ignore
        from reportlab.pdfbase.ttfonts import TTFont  # type: ignore

        pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, _DROID_FONT_PATH))
        _cjk_registered = True
        logger.info("Registered CJK font: %s", _DROID_FONT_PATH)
        return _CJK_FONT_NAME
    except Exception as exc:
        logger.warning("Failed to register CJK font: %s", exc)
        return None


def _needs_cjk(content: str) -> bool:
    """Return True if *content* contains any non-ASCII character."""
    return any(ord(ch) > 127 for ch in content)

_PAGE_MARGIN = 60  # L/R/bottom margin (pt)
_BAND_HEIGHT = 42  # saffron header band height (pt)

# Lightweight inline markdown — only **bold** and *italic*. Order matters:
# bold first so the *…* regex doesn't eat the ** markers.
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _apply_inline_markdown(text: str) -> str:
    """Convert **bold**/*italic* to reportlab's mini-HTML, escaping the rest."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _INLINE_BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_ITALIC.sub(r"<i>\1</i>", text)
    return text


def _brand_styles(cjk_font: str | None = None):
    """Build the house-style ParagraphStyle set.

    If *cjk_font* is provided (a registered TTFont name that supports CJK),
    it will be used for body, bullet, and table-cell styles so that Chinese /
    Japanese / Korean characters render correctly. Headings keep the bold
    Helvetica look (they rarely contain long CJK runs, and we lack a bold CJK
    variant).
    """
    from reportlab.lib.colors import HexColor  # type: ignore
    from reportlab.lib.styles import ParagraphStyle  # type: ignore

    body_font = cjk_font or _FONT

    def style(name, **kw):
        kw.setdefault("fontName", body_font)
        return ParagraphStyle(name, **kw)

    return {
        "h1": style(
            "h1",
            fontName=_FONT_BOLD,
            fontSize=15,
            leading=19,
            textColor=HexColor(_CACAO_DARK),
            spaceBefore=14,
            spaceAfter=6,
        ),
        "h2": style(
            "h2",
            fontName=_FONT_BOLD,
            fontSize=12.5,
            leading=16,
            textColor=HexColor(_CACAO_DARK),
            spaceBefore=12,
            spaceAfter=5,
        ),
        "h3": style(
            "h3",
            fontName=_FONT_BOLD,
            fontSize=11,
            leading=14,
            textColor=HexColor(_CACAO_MID),
            spaceBefore=10,
            spaceAfter=4,
        ),
        "body": style(
            "body", fontSize=10, leading=14.5, textColor=HexColor(_BODY), spaceAfter=4
        ),
        "bullet": style(
            "bullet",
            fontSize=10,
            leading=14.5,
            textColor=HexColor(_BODY),
            spaceAfter=3,
            leftIndent=14,
        ),
        "th": style(
            "th",
            fontName=_FONT_BOLD,
            fontSize=9,
            leading=12,
            textColor=HexColor(_CACAO_DARK),
        ),
        "td": style("td", fontSize=9, leading=12, textColor=HexColor(_BODY)),
    }


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and "|" in s and "-" in s and set(s) <= set("|-: ")


def _split_row(line: str) -> list:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _build_table(header, rows, styles, content_width):
    from reportlab.lib.colors import HexColor  # type: ignore
    from reportlab.platypus import Paragraph, Table, TableStyle  # type: ignore

    ncols = max(len(header), *(len(r) for r in rows)) if rows else len(header)
    ncols = max(ncols, 1)

    def cell(text, sty):
        return Paragraph(_apply_inline_markdown(text), sty)

    def pad(r):
        return r + [""] * (ncols - len(r))

    data = [[cell(c, styles["th"]) for c in pad(header)]]
    for r in rows:
        data.append([cell(c, styles["td"]) for c in pad(r)])

    col_w = content_width / ncols
    t = Table(data, colWidths=[col_w] * ncols, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(_RULE)),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [HexColor(_WHITE), HexColor(_ZEBRA)],
                ),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor(_RULE)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return t


def _markdown_to_flowables(markdown: str, styles, content_width) -> list:
    """Translate the markdown subset (incl. pipe tables) into reportlab flowables."""
    from reportlab.platypus import Paragraph, Spacer  # type: ignore

    flowables: list = []
    lines = markdown.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        # Pipe table: a "| ... |" line immediately followed by a |---|---| separator.
        if "|" in line and line.strip() and i + 1 < n and _is_table_sep(lines[i + 1]):
            header = _split_row(line)
            j = i + 2
            rows = []
            while j < n and "|" in lines[j] and lines[j].strip():
                rows.append(_split_row(lines[j]))
                j += 1
            flowables.append(_build_table(header, rows, styles, content_width))
            flowables.append(Spacer(1, 8))
            i = j
            continue
        if not line.strip():
            flowables.append(Spacer(1, 6))
        elif line.strip() in ("---", "***", "___"):
            flowables.append(Spacer(1, 6))  # horizontal rule → spacing
        elif line.startswith("### "):
            flowables.append(Paragraph(_apply_inline_markdown(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(_apply_inline_markdown(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flowables.append(Paragraph(_apply_inline_markdown(line[2:]), styles["h1"]))
        elif line.lstrip().startswith(("- ", "* ")):
            indent = len(line) - len(line.lstrip())
            bullet = line.lstrip()[2:]
            para = Paragraph(
                "•&nbsp;" + _apply_inline_markdown(bullet), styles["bullet"]
            )
            para.leftIndent = 14 + indent * 4
            flowables.append(para)
        else:
            flowables.append(Paragraph(_apply_inline_markdown(line), styles["body"]))
        i += 1
    return flowables


def _page_furniture(title: str):
    """Return an onPage callback that draws the saffron header band + footer."""
    from reportlab.lib.colors import HexColor  # type: ignore

    def draw(canvas, doc):
        w, h = doc.pagesize
        # Saffron header band, full width, at the top.
        canvas.setFillColor(HexColor(_SAFFRON))
        canvas.rect(0, h - _BAND_HEIGHT, w, _BAND_HEIGHT, fill=1, stroke=0)
        canvas.setFillColor(HexColor(_WHITE))
        canvas.setFont(_FONT_BOLD, 14)
        band_title = (title or "TrueSight DAO")[:90]
        canvas.drawString(_PAGE_MARGIN, h - _BAND_HEIGHT + 14, band_title)
        # Footer: muted org line + page number.
        canvas.setFillColor(HexColor(_MUTED))
        canvas.setFont(_FONT, 8)
        canvas.drawString(_PAGE_MARGIN, 28, "TrueSight DAO")
        canvas.drawRightString(w - _PAGE_MARGIN, 28, f"Page {canvas.getPageNumber()}")

    return draw


def generate_pdf(
    content: str,
    title: str | None = None,
    output_path: str | None = None,
) -> str:
    """Render ``content`` (markdown-lite) into a brand-styled PDF.

    Returns a JSON-string with ``status``, ``pdf_base64`` (capped at 256KB;
    ``truncated`` flag), ``byte_count``, and ``output_path``.
    """
    if not isinstance(content, str) or not content.strip():
        return _err("content is required")

    try:
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.platypus import SimpleDocTemplate  # type: ignore
    except Exception as e:  # pragma: no cover
        return _err(f"reportlab unavailable: {e}")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            prefix="autopilot_pdf_", suffix=".pdf", delete=False
        )
        tmp.close()
        output_path = tmp.name

    # Detect CJK content and register fallback font if needed.
    cjk_font = _ensure_cjk_font() if _needs_cjk(content) else None

    buf = io.BytesIO()
    styles = _brand_styles(cjk_font=cjk_font)
    page_w, page_h = LETTER
    content_width = page_w - 2 * _PAGE_MARGIN

    try:
        doc = SimpleDocTemplate(
            buf,
            pagesize=LETTER,
            title=title or "TrueSight DAO — autopilot output",
            author="TrueSight DAO Autopilot",
            leftMargin=_PAGE_MARGIN,
            rightMargin=_PAGE_MARGIN,
            topMargin=_BAND_HEIGHT + 24,  # clear the saffron band
            bottomMargin=48,
        )
        flowables = _markdown_to_flowables(content, styles, content_width)
        if len(flowables) > _HARD_PAGE_LIMIT * 60:
            flowables = flowables[: _HARD_PAGE_LIMIT * 60]
        furniture = _page_furniture(title or "")
        doc.build(flowables, onFirstPage=furniture, onLaterPages=furniture)
    except Exception as e:
        return _err(f"PDF rendering failed: {e}")

    pdf_bytes = buf.getvalue()

    try:
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        return _err(f"failed to write output_path: {e}", output_path=output_path)

    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    truncated = len(encoded) > _MAX_BYTES_RETURN
    returned_b64 = encoded[:_MAX_BYTES_RETURN] if truncated else encoded

    logger.info(
        "generate_pdf ok: title=%s bytes=%d truncated=%s path=%s",
        (title or "")[:60],
        len(pdf_bytes),
        truncated,
        output_path,
    )
    return json.dumps(
        {
            "status": "ok",
            "title": title or "",
            "byte_count": len(pdf_bytes),
            "pdf_base64": returned_b64,
            "truncated": truncated,
            "output_path": output_path,
        }
    )


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="generate_pdf",
    description="Render markdown-lite content into a brand-styled (Saffron Monk) PDF: saffron header band with the title, Helvetica text, cacao headings, and Markdown pipe-tables rendered as REAL tables (gray header + zebra rows). Supports # / ## / ### headings, paragraphs, '- '/'* ' bullets, **bold**, *italic*, and | a | b | + |---|---| tables. Returns base64 PDF bytes (capped 256KB; full file at output_path). Pair with upload_file_to_github(content_base64=...).",
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Markdown-lite source: # / ## / ### headings, blank-line paragraphs, '- '/'* ' bullets, **bold**, *italic*, and pipe tables (| a | b | then |---|---|).",
            },
            "title": {
                "type": "string",
                "description": "Document title — shown on the saffron header band on every page.",
            },
            "output_path": {
                "type": "string",
                "description": "Optional local path to write the full PDF to (default: auto-generated /tmp file).",
            },
        },
        "required": ["content"],
    },
    handler=lambda args, ctx: generate_pdf(
        content=args.get("content", ""),
        title=args.get("title"),
        output_path=args.get("output_path"),
    ),
)
