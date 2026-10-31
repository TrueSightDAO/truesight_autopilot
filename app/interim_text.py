"""Recognise Sophia's own interim progress placeholders.

Why this module exists (2026-10-31): the per-turn ``Thinking...`` placeholder
is posted via ``send_message``, which flagged it resume-awaiting. On a turn
longer than ``DEFAULT_AWAITING_FRESH_S`` (3600s) that placeholder aged from a
*fresh* prompt into ``awaiting_stale``, so the loop nagged the governor about a
decision that did not exist. A progress line is not a question.

Deliberately an **exact allowlist**, not a verb pattern. An earlier draft
matched ``<emoji> (Thinking|Running|...)`` and therefore also swallowed a
genuine reply such as ``"\\U0001f504 Running the deploy now, will report"`` --
suppressing the mark on a real answer silently removes the governor's
reaction-to-resume affordance. Only strings Sophia itself emits qualify.

Kept dependency-free (stdlib only) so :mod:`app.loop_status` can import it and
stay pure/unit-testable without pulling in the Discord adapter or credentials.
"""

from __future__ import annotations

import re

# Exact placeholders Sophia posts as machinery. The trailing ellipsis is
# U+2026 HORIZONTAL ELLIPSIS, which is what the call sites actually send.
_INTERIM_EXACT: frozenset[str] = frozenset(
    {
        "\U0001f504 Thinking\u2026",  # \U0001f504 Thinking\u2026        (turn start)
        "\U0001f4c4 Processing attachment\u2026",  # \U0001f4c4 Processing attachment\u2026
    }
)

# The streaming loop re-posts the same placeholder with a round counter:
# "\U0001f504 Thinking\u2026 (round 5)".
_INTERIM_ROUND_RE = re.compile("^\U0001f504 Thinking\u2026 \\(round \\d+\\)$")


def is_interim_text(text: str) -> bool:
    """True only for an exact interim placeholder Sophia posted itself."""
    candidate = (text or "").strip()
    if not candidate:
        return False
    if candidate in _INTERIM_EXACT:
        return True
    return bool(_INTERIM_ROUND_RE.match(candidate))
