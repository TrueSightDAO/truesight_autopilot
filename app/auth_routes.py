"""Auth routes for vault login — REMOVED 2026-10-24 (security).

This module previously exposed two unauthenticated, internet-reachable routes:

- ``POST /auth/send-challenge`` — accepted an arbitrary string as "email" with
  no format validation, no rate limiting, and no registered-user check, and
  sent a Gmail message to it from the admin mailbox.
- ``POST /auth/verify-code`` — compared the submitted code and, on a match,
  minted a JWT via ``auth.create_jwt(synthetic_key)``.

Why they were deleted
---------------------
1. **Privilege escalation.** ``create_jwt`` defaults ``author_role`` to
   ``DEFAULT_AUTHOR_ROLE == "governor"``. ``verify_code`` passed no
   ``author_role``, so any email that completed the challenge received a token
   asserting the ``governor`` role. ``main.py`` forces that claim onto the
   policy identity (``role=_Role(author_role)``) before evaluating tool
   permissions, so a token from this flow evaluated as a full governor
   (``submit_contribution`` / ``git_push_changes`` / ``ssh_run`` / etc. all
   allowed).
2. **Unauthenticated mail relay.** ``send-challenge`` took any string as a
   recipient and sent mail from the admin Gmail account.
3. **No legitimate consumers.** The frontend authenticates via
   ``/vault/api/verify-signature`` (RSA key signature). Nothing in
   ``app/static``, ``app/templates``, or any deployed page called either route.
   Only automated scanners ever hit them.

A note on ``gmail_send``'s return type
--------------------------------------
The removed ``send-challenge`` handler called ``result.get("returncode")`` on
``gmail_send``'s result. ``gmail_send`` returns a JSON **string** (every return
path is ``json.dumps(...)`` or ``_err(...)``), so that line raised
``AttributeError: 'str' object has no attribute 'get'`` — *after* the message
had already been sent. The defect was the caller assuming a dict, not the
tool's contract; callers must ``json.loads`` the result. Refactoring
``gmail_send`` to return a dict touches every consumer and is tracked
separately.

The ``router`` object is kept so existing imports remain valid. Do not
re-register these routes. If email-based vault login is ever genuinely needed,
validate the address format, require the email to belong to a *registered*
governor, rate-limit per IP and per address, and pass an explicit
``author_role="guest"`` to ``create_jwt`` — never rely on its default.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])
