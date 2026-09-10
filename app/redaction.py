"""Secret redaction (single source of truth).

Extracted from ``app.main`` so the email-watch dispatcher can scrub outbound
bodies (security policy rule 5) WITHOUT importing the whole FastAPI app (and
without an import cycle once the watch loop is wired into main's lifespan).

``app.main._redact_secrets`` is now a thin lazy wrapper over this module, so
``from app.main import _redact_secrets`` keeps working for existing callers.
"""

from __future__ import annotations

import re

REDACTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:AWS_ACCESS_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:LLM_API_KEY]"),
    (
        re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36,}\b"),
        "[REDACTED:GITHUB_TOKEN]",
    ),
    (
        re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
        "[REDACTED:GITHUB_FINE_GRAINED_PAT]",
    ),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{20,}"), "[REDACTED:SLACK_TOKEN]"),
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY[ -]*-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY[ -]*-----"
        ),
        "[REDACTED:PRIVATE_KEY_BLOCK]",
    ),
    # JWT-shaped tokens: 3 base64url segments separated by dots, each at least 8 chars
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
        "[REDACTED:JWT]",
    ),
    # env-style line: KEY_LIKE=secret-shaped-value (long, no spaces, base64-ish)
    (
        re.compile(r"^([A-Z][A-Z0-9_]{3,})=([A-Za-z0-9+/=_\-]{20,})$", re.MULTILINE),
        r"\1=[REDACTED:ENV_VALUE]",
    ),
]


def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Redact common secret-shaped strings before sending/publishing.

    Returns ``(redacted_text, list_of_categories_that_matched)``.
    """
    if not text:
        return text, []
    matched: list[str] = []
    for pattern, replacement in REDACTION_PATTERNS:
        if pattern.search(text):
            tag = (
                replacement.split(":", 1)[1].rstrip("]")
                if ":" in replacement
                else replacement
            )
            matched.append(tag)
            text = pattern.sub(replacement, text)
    return text, matched
