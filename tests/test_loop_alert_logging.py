"""The alert runner must not write request URLs (with tokens) to the journal.

Regression: ``basicConfig(level=INFO)`` enabled ``httpx``'s request logging, and
a Telegram send logs its full URL -- which embeds the bot token. Under systemd
those lines went to journald in cleartext.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "loop_alert.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "loop_alert_script_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("verbose", [False, True])
def test_request_loggers_are_silenced(verbose):
    mod = _load()
    mod._configure_logging(verbose=verbose)
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), f"{name} still logs INFO"
        assert logger.isEnabledFor(logging.WARNING)


def test_silencing_is_scoped_to_the_transport():
    """Raising httpx/httpcore must not deafen the runner's own logging.

    Root is set explicitly first: pytest installs its own handlers, which makes
    ``basicConfig`` a no-op, so relying on it would test pytest's setup rather
    than this code.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # a fresh interpreter starts permissive
    mod = _load()
    mod._configure_logging(verbose=True)
    assert root.isEnabledFor(logging.DEBUG), "runner logging was globally deafened"
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
