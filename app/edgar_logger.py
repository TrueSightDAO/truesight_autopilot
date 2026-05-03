"""Log autopilot actions as [CONTRIBUTION EVENT] to Edgar."""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

logger = logging.getLogger("autopilot.edgar")


class EdgarLogger:
    """Thin wrapper around dao_client to submit contribution events."""

    def __init__(self):
        self._dao_client_path = self._find_dao_client()

    def _find_dao_client(self) -> Path | None:
        """Locate dao_client repo relative to this project."""
        candidates = [
            Path(__file__).resolve().parents[2] / "dao_client",
            Path.home() / "Applications" / "dao_client",
        ]
        for p in candidates:
            if (p / "truesight_dao_client" / "modules" / "report_contribution.py").exists():
                return p
        return None

    def log_contribution(
        self,
        minutes: int,
        description: str,
        pr_url: str | None = None,
    ) -> bool:
        """Submit a [CONTRIBUTION EVENT] via dao_client."""
        if not self._dao_client_path:
            logger.warning("dao_client not found — skipping Edgar log")
            return False
        if not all([settings.email, settings.public_key, settings.private_key]):
            logger.warning("Edgar credentials incomplete — skipping")
            return False

        # Build command
        cmd = [
            "python",
            "-m",
            "truesight_dao_client.modules.report_contribution",
            "--type", "Time (Minutes)",
            "--amount", str(minutes),
            "--description", description,
            "--contributors", "truesight-autopilot",
        ]
        if pr_url:
            cmd.extend(["--attr", f"PR URL={pr_url}"])

        env = {
            **dict(subprocess.os.environ),
            "EMAIL": settings.email,
            "PUBLIC_KEY": settings.public_key,
            "PRIVATE_KEY": settings.private_key,
        }

        try:
            result = subprocess.run(
                cmd,
                cwd=str(self._dao_client_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info("Edgar contribution logged: %s", description)
                return True
            else:
                logger.error("Edgar submission failed: %s", result.stderr)
                return False
        except Exception as e:
            logger.error("Edgar submission exception: %s", e)
            return False
