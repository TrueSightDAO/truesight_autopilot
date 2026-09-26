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
        return self._client is not None and all(
            [settings.email, settings.public_key, settings.private_key]
        )

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
                logger.info(
                    "Edgar contribution submitted: %s", description or event_name
                )
                return True
            else:
                logger.error(
                    "Edgar submission failed (%d): %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return False
        except Exception as e:
            logger.error("Edgar submission exception: %s", e)
            return False

    def register_qr_code(self, attributes: dict[str, object]) -> bool:
        """Submit a QR code registration event to Edgar, then trigger GAS."""
        if not self.is_configured():
            logger.warning("Edgar credentials incomplete — skipping")
            return False

        try:
            # Step 1: POST to Edgar
            payload, request_txn_id, share_text = self._client.sign(
                event_name="QR CODE REGISTRATION",
                attributes=attributes,
            )
            resp = self._client.session.post(
                f"{self._client.base_url}/dao/qr_code_register",
                data={"text": share_text},
                timeout=30.0,
            )
            if not resp.ok:
                logger.error(
                    "Edgar QR registration failed (%d): %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return False

            # Step 2: Trigger GAS processing
            import requests as _requests

            gas_url = (
                "https://script.google.com/macros/s/"
                "AKfycbzlUS6-b3_wZaGwTVenx3pBNNNScGDt9TB0ueUyDPvbkt64zryH5QI_hrvT7i2EPYEc"
                "/exec?action=processQRCodeGenerationTelegramLogs"
            )
            gas_resp = _requests.get(gas_url, timeout=60)
            if gas_resp.ok:
                logger.info("GAS processing triggered for QR registration")
            else:
                logger.warning(
                    "GAS trigger returned %d: %s",
                    gas_resp.status_code,
                    gas_resp.text[:200],
                )

            return True
        except Exception as e:
            logger.error("QR registration exception: %s", e)
            return False

    def log_contribution(
        self, minutes: int, description: str, pr_url: str | None = None
    ) -> bool:
        """Convenience: log an autopilot fix as a time-based contribution."""
        attrs: dict[str, object] = {
            "Type": "Time (Minutes)",
            "Amount": str(minutes),
            "Description": description[:200],
            "Contributors": "truesight-autopilot",
        }
        if pr_url:
            attrs["PR URL"] = pr_url
        return self.submit_contribution(
            "CONTRIBUTION EVENT", attrs, description=description
        )
