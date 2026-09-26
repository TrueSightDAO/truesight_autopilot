#!/usr/bin/env python3
"""One-time interactive Telethon login for the attention watchdog.

Creates the MTProto user-session file that ``app/attention_watchdog.py``
needs. Run this ONCE, interactively, on the box that will run the watchdog
(the autopilot EC2), as the same user/cwd the systemd unit uses:

    cd /opt/truesight_autopilot
    .venv/bin/python scripts/telethon_login.py

Prerequisites (in /opt/truesight_autopilot/.env):
    TELEGRAM_API_ID=...    # from https://my.telegram.org → API development tools
    TELEGRAM_API_HASH=...  # same page

You will be prompted for your phone number (international format), the login
code Telegram sends to your app, and your 2FA password if you have one. The
resulting ``.telethon_watchdog.session`` file is FULL access to your account —
it stays on this box, is never committed (gitignored), and deploy.sh only
starts the watchdog unit when it exists.

To revoke later: Telegram app → Settings → Devices → terminate the session.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402


def main() -> int:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env — get them at\n"
            "https://my.telegram.org (API development tools), add to .env, re-run."
        )
        return 1
    from telethon.sync import TelegramClient

    with TelegramClient(
        settings.watchdog_session_path,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    ) as client:
        me = client.get_me()
        print(
            f"\nAuthorized as {me.first_name or ''} {me.last_name or ''} "
            f"(@{me.username}) — session saved to "
            f"{settings.watchdog_session_path}.session"
        )
        client.send_message(
            "me",
            "👋 Attention watchdog session created. Sophia will nudge you here "
            "about unanswered asks. Start the service with:\n"
            "sudo systemctl start truesight-autopilot-watchdog",
        )
        print("Sent a confirmation to your Saved Messages.")
    print("\nNow: sudo systemctl enable --now truesight-autopilot-watchdog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
