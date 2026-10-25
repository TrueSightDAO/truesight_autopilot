"""Deploy-completion notification fan-out.

``main._check_deploy_marker()`` runs in the freshly-booted process after a
redeploy and has historically notified only Telegram (the originating topic).
This module adds the Discord mirror: every deploy completion is also posted to
a dedicated ops channel so the whole guild sees deploy state, not just the
governor's Telegram thread.

Disabled by default -- set ``DEPLOY_NOTIFY_DISCORD_CHANNEL_ID`` to a channel id
to enable. Never raises: a notification failure must not affect boot.
"""

from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger("autopilot.deploy_notify")


def notify_deploy_complete_discord(
    commit: str,
    elapsed_seconds: float,
    channel_id: str | int | None = None,
) -> bool:
    """Post a 'deploy complete' line to the configured Discord channel.

    Returns True when a message was posted, False when skipped (no channel
    configured) or on any error. Never raises.
    """
    chan = channel_id
    if chan is None:
        chan = getattr(settings, "deploy_notify_discord_channel_id", "")
    chan = str(chan or "").strip()
    if not chan:
        logger.debug(
            "deploy_notify: DEPLOY_NOTIFY_DISCORD_CHANNEL_ID unset -- skipping"
        )
        return False

    commit_short = commit[:7] if commit and commit != "unknown" else "unknown"
    text = (
        "\u2705 **Autopilot deploy complete**\n"
        f"\u2022 Commit: `{commit_short}`\n"
        f"\u2022 Back online in {elapsed_seconds}s"
    )
    try:
        from . import discord_adapter

        ids = discord_adapter.send_message(chan, text)
        if ids:
            logger.info("deploy_notify: posted deploy-complete to Discord %s", chan)
            return True
        logger.warning("deploy_notify: Discord post returned no ids (channel %s)", chan)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("deploy_notify: Discord post failed: %s", e)
        return False
