"""ssh_run must block raw self-restart/kill of the autopilot (2026-06-12).

Those commands bypass deploy_autopilot's idle-drain guard and brick active
threads. The only sanctioned restart path is the deploy_autopilot tool.
"""

from __future__ import annotations

import pytest

try:
    from app.tools import ssh_tools as st
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"ssh_tools import unavailable: {exc}", allow_module_level=True)


BLOCKED = [
    "sudo systemctl restart truesight-autopilot",
    "systemctl restart truesight-autopilot-telegram",
    "sudo systemctl stop truesight-autopilot truesight-autopilot-telegram",
    "systemctl kill -s SIGKILL truesight-autopilot",
    "pkill -f uvicorn",
    "pkill -9 -f app.main",
    "killall uvicorn",
    "kill -9 $(pgrep -f app.main)",
]
ALLOWED = [
    "systemctl status truesight-autopilot",
    "sudo systemctl restart nginx",
    "git pull origin main",
    "ruff check app tests",
    "ls -la /opt/truesight_autopilot",
    "journalctl -u truesight-autopilot -n 20",
]


def test_regex_blocks_self_restart():
    for c in BLOCKED:
        assert st._SELF_RESTART_RE.search(c), f"should block: {c}"


def test_regex_allows_benign():
    for c in ALLOWED:
        assert not st._SELF_RESTART_RE.search(c), f"should allow: {c}"


def test_ssh_run_refuses_self_restart_before_ssh():
    # Returns the block error WITHOUT attempting any SSH (no key/network needed).
    out = st.ssh_run("autopilot", "sudo systemctl restart truesight-autopilot")
    assert out.get("status") == "error"
    assert "BLOCKED" in out.get("reason", "")
    assert "deploy_autopilot" in out.get("reason", "")
