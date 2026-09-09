"""Hourly watch over the admin+sophia@truesight.me inbox (plan Unit 1 skeleton).

SOPHIA_EMAIL_INBOX_WATCH_PLAN.md - this module is the loop skeleton: it lists
unread plus-alias mail and logs dispositions. NO sends and NO internal
chat-turn dispatch yet (Units 2-3 add those under the security policy). It is
not wired into main.py until Unit 4, so it is inert in production.

Design:
- Mirrors app/email_poller.py's run_loop/poll_once shape (own asyncio loop,
  own interval). NOT followups.py, which is purpose-built for OPEN_FOLLOWUPS.md.
- Scoped to the plus-alias by query: plus-aliases share the admin mailbox
  (Unit 0 confirmed admin_token.json authenticates as admin@truesight.me), so
  the Gmail query is the scoping mechanism.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("autopilot.email_inbox_watch")

# Scopes this watch to the admin+sophia@ inbox view. is:unread keeps each tick
# cheap; idempotent per-thread handling state arrives with Unit 2's dispatch.
INBOX_QUERY = "to:admin+sophia@truesight.me is:unread"
DEFAULT_INTERVAL_SECONDS = 3600  # hourly (plan trigger)


class EmailInboxWatch:
    def __init__(self) -> None:
        self._search_fn = None  # lazy import, see _search()

    def _search(self):
        """Return the gmail_search tool function (imported lazily).

        Kept behind a method so tests can monkeypatch it without importing the
        google client stack at module load time.
        """
        if self._search_fn is None:
            from .tools.gmail_tools import gmail_search

            self._search_fn = gmail_search
        return self._search_fn

    async def run_loop(self, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
        """Poll the inbox every hour (Unit 1: observe-and-log only)."""
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:
                logger.exception("Email inbox watch poll failed")
            await asyncio.sleep(interval_seconds)

    def poll_once(self) -> int:
        """List unread admin+sophia@ mail and log each message.

        Returns the number of unread messages found. Unit 1 makes NO changes:
        no dispatch, no sends, no mark-read - every unread message is left
        UNREAD so a dry-run review day (plan Unit 2/3 gate) sees real traffic.
        """
        raw = self._search()(query=INBOX_QUERY, account="admin", max_results=20)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("gmail_search returned non-JSON: %.200s", raw)
            return 0
        if payload.get("status") != "ok":
            logger.error("gmail_search failed: %s", payload.get("reason"))
            return 0
        results = payload.get("results", [])
        for r in results:
            logger.info(
                "[email-inbox-watch] unread: id=%s thread=%s from=%s subject=%s",
                r.get("id"),
                r.get("thread_id"),
                r.get("from"),
                r.get("subject"),
            )
        if results:
            logger.info(
                "[email-inbox-watch] found %d unread - Unit 1 observe-only, "
                "leaving UNREAD (dispatch lands in Unit 2)",
                len(results),
            )
        return len(results)
