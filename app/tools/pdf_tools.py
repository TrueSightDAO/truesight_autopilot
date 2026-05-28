"""Generate a PDF from text/markdown for the autopilot agent.

Uses ``reportlab`` (pure-Python, no system deps) to render content into a
multi-page PDF. The output is returned as base64-encoded bytes so the agent
can either:

- Feed the base64 directly to ``upload_file_to_github`` via its ``content_base64``
  argument to ship the PDF into a TrueSightDAO repo, or
- Save it locally via ``write_local_file`` (planned) for further processing.

Markdown is rendered with a deliberately light hand: headings (``#``, ``##``,
``###``), paragraphs, bullets (``-``/``*``), and inline ``**bold**`` /
``*italic*``. Anything more elaborate (tables, code blocks, links rendered
as anchors) is rendered as preformatted text to keep the implementation small
and predictable.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import tempfile
from typing import Any

logger = logging.getLogger("autopilot.tools.pdf_tools")

_MAX_BYTES_RETURN = 256 * 1024  # cap on base64 returned to the model
_HARD_PAGE_LIMIT = 200  # safety net for runaway loops

# Lightweight inline markdown — only **bold** and *italic*. Order matters:
# bold first so the *…* regex doesn't eat the ** markers.
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _apply_inline_markdown(text: str) -> str:
    """Convert **bold**/*italic* to reportlab's mini-HTML."""
    text = _INLINE_BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_ITALIC.sub(r"<i>\1</i>", text)
    # ReportLab Paragraph requires < > & escaped — but we just inserted real
    # tags. Escape any remaining bare angle brackets / ampersands that aren't
    # part of our generated tags.
    text = text.replace("&", "&amp;")
    text = re.sub(r"&amp;(b|i|/b|/i);", lambda m: "&" + m.group(1) + ";", text)
    # Re-create our own tags (we replaced & with &amp; above; restore < > on the
    # generated b/i tags only).
    text = re.sub(r"&lt;(/?(?:b|i))&gt;", r"<\1>", text)
    return text


def _markdown_to_flowables(markdown: str, styles) -> list:
    """Translate a tiny markdown subset into reportlab flowables."""
    from reportlab.platypus import Paragraph, Spacer  # type: ignore

    flowables: list = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flowables.append(Spacer(1, 8))
            continue
        if line.startswith("### "):
            flowables.append(Paragraph(_apply_inline_markdown(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(_apply_inline_markdown(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            flowables.append(Paragraph(_apply_inline_markdown(line[2:]), styles["h1"]))
        elif line.lstrip().startswith(("- ", "* ")):
            indent = len(line) - len(line.lstrip())
            bullet = line.lstrip()[2:]
            para = Paragraph("• " + _apply_inline_markdown(bullet), styles["bullet"])
            para.leftIndent = 12 + indent * 4
            flowables.append(para)
        else:
            flowables.append(Paragraph(_apply_inline_markdown(line), styles["body"]))
    return flowables


def generate_pdf(
    content: str,
    title: str | None = None,
    output_path: str | None = None,
) -> str:
    """Render ``content`` (markdown-lite) into a PDF.

    Returns a JSON-string with:
    - ``status``
    - ``pdf_base64``  — capped at 256KB; ``truncated`` flag set if exceeded
    - ``byte_count``  — full size of the rendered PDF
    - ``output_path`` — the temp file the PDF was written to (so the agent
      can pass it to other tools without re-shipping the bytes)
    """
    if not isinstance(content, str) or not content.strip():
        return _err("content is required")

    try:
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
        from reportlab.platypus import SimpleDocTemplate  # type: ignore
    except Exception as e:  # pragma: no cover
        return _err(f"reportlab unavailable: {e}")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(prefix="autopilot_pdf_", suffix=".pdf", delete=False)
        tmp.close()
        output_path = tmp.name

    buf = io.BytesIO()
    base_styles = getSampleStyleSheet()
    # Re-key heading sizes so they look like a normal document.
    styles = {
        "h1": base_styles["Heading1"],
        "h2": base_styles["Heading2"],
        "h3": base_styles["Heading3"],
        "body": base_styles["BodyText"],
        "bullet": base_styles["BodyText"],
    }

    try:
        doc = SimpleDocTemplate(
            buf,
            pagesize=LETTER,
            title=title or "TrueSight DAO — autopilot output",
            author="TrueSight DAO Autopilot",
        )
        flowables = _markdown_to_flowables(content, styles)
        # Guard against runaway flowables eating memory.
        if len(flowables) > _HARD_PAGE_LIMIT * 60:
            flowables = flowables[: _HARD_PAGE_LIMIT * 60]
        doc.build(flowables)
    except Exception as e:
        return _err(f"PDF rendering failed: {e}")

    pdf_bytes = buf.getvalue()

    # Write the full PDF to disk so the agent can attach/upload it via
    # output_path without re-streaming.
    try:
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        return _err(f"failed to write output_path: {e}", output_path=output_path)

    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    truncated = len(encoded) > _MAX_BYTES_RETURN
    returned_b64 = encoded[: _MAX_BYTES_RETURN] if truncated else encoded

    logger.info(
        "generate_pdf ok: title=%s bytes=%d truncated=%s path=%s",
        (title or "")[:60], len(pdf_bytes), truncated, output_path,
    )
    return json.dumps({
        "status": "ok",
        "title": title or "",
        "byte_count": len(pdf_bytes),
        "pdf_base64": returned_b64,
        "truncated": truncated,
        "output_path": output_path,
    })
