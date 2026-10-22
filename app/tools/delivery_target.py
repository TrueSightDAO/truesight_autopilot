"""Decide which chat platform a session belongs to, so attachment tools can route.

The 2026-10-22 incident: a governor working in a **Discord** session asked
Sophia to "share the PDF here". She called ``send_telegram_attachment``; that
tool looked for a ``tg:`` component in the session key, found none, and fell
back to ``settings.telegram_home_group_id`` -- posting the file into a Telegram
group the governor was not looking at. The tool returned ``status: ok`` with
``message_thread_id: null``. It was telling the truth about the wrong place.

Session keys carry an unambiguous platform marker:

    Telegram  ``<pk[:20]>:tg:<chat_id>:<thread_id>``    (telegram_adapter)
    Discord   ``<pk[:20]>:dc:<guild_id>:<channel_id>``  (discord_adapter)

``platform_from_session`` reads that marker so a sender can reroute instead of
guessing. This module imports no platform SDK and no sibling tool module at
import time, so both attachment tools can depend on it without a cycle.
"""

from __future__ import annotations

import re

_TG_RE = re.compile(r"(?:^|:)tg:-?\d+")
_DC_RE = re.compile(r"(?:^|:)dc:-?\d+")
_TG_PARTS = re.compile(r"(?:^|:)tg:(-?\d+)(?::(-?\d+))?")
_DC_PARTS = re.compile(r"(?:^|:)dc:(\d+):(\d+)")


def platform_from_session(session_id: str | None) -> str | None:
    """Return ``"telegram"``, ``"discord"``, or None for the given session key.

    None means the key carries no platform marker (a DApp/web session, or no
    session at all) -- callers may then fall back to their configured home.
    A key that genuinely carries both markers is treated as ambiguous and also
    returns None, so the sender refuses rather than picking blindly.
    """
    sid = session_id or ""
    has_tg = bool(_TG_RE.search(sid))
    has_dc = bool(_DC_RE.search(sid))
    if has_tg and has_dc:
        return None
    if has_tg:
        return "telegram"
    if has_dc:
        return "discord"
    return None


def describe_destination(session_id: str | None) -> dict:
    """Human/machine readable view of where this session points.

    Useful in tool output so a wrong destination is visible in the result
    instead of silent. Never raises.
    """
    sid = session_id or ""
    platform = platform_from_session(sid)
    out: dict = {"platform": platform}
    if platform == "telegram":
        m = _TG_PARTS.search(sid)
        if m:
            out["chat_id"] = m.group(1)
            try:
                out["thread_id"] = int(m.group(2)) if m.group(2) else None
            except (TypeError, ValueError):
                out["thread_id"] = None
    elif platform == "discord":
        m = _DC_PARTS.search(sid)
        if m:
            out["guild_id"] = m.group(1)
            out["channel_id"] = m.group(2)
    return out
