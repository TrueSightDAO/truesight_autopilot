"""Recognise Sophia's own interim progress placeholders.

Why this module exists (2026-10-31): the per-turn ``Thinking...`` placeholder
is posted via ``send_message``, which flagged it resume-awaiting. On a turn
longer than ``DEFAULT_AWAITING_FRESH_S`` (3600s) that placeholder aged from a
*fresh* prompt into ``awaiting_stale``, so the loop nagged the governor about a
decision that did not exist. A progress line is not a question.

Second defect (same day): the first cut recognised only two exact strings, so
it caught the ``Thinking...`` line but not the per-tool-call placeholders
(``ssh run``, ``merge pr``, ``pin note`` ...). Replayed against the live
registry, it filtered 21 entries and let 63 through -- so a long turn still
nagged. :func:`_is_tool_progress` closes that gap.

Both rules are deliberately narrow. An earlier draft matched
``<emoji> (Thinking|Running|...)`` and therefore also swallowed a genuine reply
such as ``"Running the deploy now, will report"`` -- suppressing the mark on a
real answer silently removes the governor's reaction-to-resume affordance.
Only strings Sophia itself emits qualify.

Kept dependency-free (stdlib only) so :mod:`app.loop_status` can import it and
stay pure/unit-testable without pulling in the Discord adapter or credentials.
"""

from __future__ import annotations

import re

# Exact placeholders Sophia posts as machinery. The trailing ellipsis is
# U+2026 HORIZONTAL ELLIPSIS, which is what the call sites actually send.
_INTERIM_EXACT: frozenset[str] = frozenset(
    {
        "\U0001f504 Thinking\u2026",
        "\U0001f4c4 Processing attachment\u2026",
    }
)

# The streaming loop re-posts the same placeholder with a round counter:
# "Thinking... (round 5)".
_INTERIM_ROUND_RE = re.compile("^\U0001f504 Thinking\u2026 \\(round \\d+\\)$")

# Per-tool-call progress lines look like "<emoji> <tool name> <U+2026>", e.g.
# "\u2699\ufe0f ssh run \u2026" or "\u2705 merge pr \u2026". The tool name is machine
# generated and always lower case; human prose is not.
_LEADING_EMOJI_RE = re.compile(
    "^[\u2600-\u27bf\u2b00-\u2bff\U0001f000-\U0001faff]"
)
_LEADING_NON_WORD_RE = re.compile(r"^[^\w]+")
_INTERIM_MAX_LEN = 80


def _is_tool_progress(candidate: str) -> bool:
    """True for a machine-generated per-tool-call progress line.

    Signature: leading emoji, short, ends with U+2026, and the text after the
    emoji is entirely lower case. ``"\u2699\ufe0f ssh run ..."`` (ASCII dots) is not
    matched -- the call sites emit U+2026 -- and neither is a real reply that
    merely begins with an emoji.
    """
    if len(candidate) > _INTERIM_MAX_LEN:
        return False
    if not candidate.endswith("\u2026"):
        return False
    if not _LEADING_EMOJI_RE.match(candidate):
        return False
    body = _LEADING_NON_WORD_RE.sub("", candidate[: -len("\u2026")]).strip()
    # All lower case separates "merge pr" (machinery) from real prose.
    return bool(body) and body == body.lower()


def is_interim_text(text: str) -> bool:
    """True only for an exact interim placeholder Sophia posted itself."""
    candidate = (text or "").strip()
    if not candidate:
        return False
    if candidate in _INTERIM_EXACT:
        return True
    if _INTERIM_ROUND_RE.match(candidate):
        return True
    return _is_tool_progress(candidate)
