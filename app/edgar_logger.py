"""Log autopilot actions as [CONTRIBUTION EVENT] to Edgar via dao_client library."""

from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger("autopilot.edgar")


class EdgarLogger:
    """Thin wrapper around dao_client EdgarClient to submit contribution events."""

    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            from truesight_dao_client.edgar_client import EdgarClient

            self._client = EdgarClient(
                email=settings.email,
                public_key_b64=settings.public_key,
                private_key_b64=settings.private_key,
                generation_source="https://github.com/TrueSightDAO/truesight_autopilot",
            )
        except Exception as e:
            logger.warning("EdgarClient init failed: %s", e)

    def is_configured(self) -> bool:
        return self._client is not None and all([settings.email, settings.public_key, settings.private_key])

    def submit_contribution(
        self,
        event_name: str,
        attributes: dict[str, object],
        description: str = "",
    ) -> bool:
        """Submit a signed contribution event directly to Edgar."""
        if not self.is_configured():
            logger.warning("Edgar credentials incomplete — skipping")
            return False

        try:
            resp = self._client.submit(event_name, attributes)
            if resp.ok:
                logger.info("Edgar contribution submitted: %s", description or event_name)
                return True
            else:
                logger.error("Edgar submission failed (%d): %s", resp.status_code, resp.text[:300])
                return False
        except Exception as e:
            logger.error("Edgar submission exception: %s", e)
            return False

    def log_contribution(self, minutes: int, description: str, pr_url: str | None = None) -> bool:
        """Convenience: log an autopilot fix as a time-based contribution."""
        attrs: dict[str, object] = {
            "Type": "Time (Minutes)",
            "Amount": str(minutes),
            "Description": description[:200],
            "Contributors": "truesight-autopilot",
        }
        if pr_url:
            attrs["PR URL"] = pr_url
        return self.submit_contribution("CONTRIBUTION EVENT", attrs, description=description)
