"""Guard the loop-daemon systemd unit.

The unit is installed by hand (not by deploy.sh), so nothing else would catch a
regression in it. These assertions encode the three things that actually bit us:
absolute paths (relative fallbacks fail silently), the venv interpreter, and the
deploy-order warning (a missing module under Restart=always is a restart spin).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNIT = (
    Path(__file__).resolve().parent.parent / "systemd" / "truesight-loop-daemon.service"
)
LIVE_DIR = "/opt/truesight_autopilot"


@pytest.fixture(scope="module")
def unit() -> configparser.ConfigParser:
    assert UNIT.exists(), f"missing unit file: {UNIT}"
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.read(UNIT)
    return cp


def test_required_sections(unit: configparser.ConfigParser) -> None:
    for section in ("Unit", "Service", "Install"):
        assert section in unit, f"missing [{section}]"


def test_runs_the_venv_interpreter_and_the_daemon_module(
    unit: configparser.ConfigParser,
) -> None:
    exec_start = unit["Service"]["ExecStart"]
    assert exec_start.startswith(f"{LIVE_DIR}/.venv/bin/python"), exec_start
    assert "-m app.loop_daemon" in exec_start, exec_start


def test_working_directory_is_the_live_checkout(
    unit: configparser.ConfigParser,
) -> None:
    assert unit["Service"]["WorkingDirectory"] == LIVE_DIR


def test_absolute_paths_are_pinned(unit: configparser.ConfigParser) -> None:
    """Relative fallbacks degrade silently to 'no sessions' — pin them.

    Read the RAW text, not ConfigParser: systemd accumulates repeated
    ``Environment=`` lines, but configparser keeps only the LAST duplicate key,
    so a ConfigParser-based assertion would silently ignore the first one.
    """
    text = UNIT.read_text()
    assert f"Environment=ACTIVE_TRACKS_PATH={LIVE_DIR}/data/active_tracks.json" in text
    assert f"Environment=SESSION_LOG_DIR={LIVE_DIR}/sessions" in text


def test_carries_the_deploy_order_warning(unit: configparser.ConfigParser) -> None:
    """Restart=always + a missing module = a 30s restart spin. The warning is the
    only thing standing between a fresh host and that loop, so assert it stays."""
    text = UNIT.read_text()
    assert "Restart=always" in text
    assert "Do NOT enable/start it before that code is actually deployed" in text


def test_does_not_cycle_the_autopilot_service(unit: configparser.ConfigParser) -> None:
    """A reconciler must never be able to restart the app it observes."""
    text = UNIT.read_text()
    assert "ExecStartPre" not in text
    assert "ExecStopPost" not in text
    assert "truesight-autopilot.service" not in text
