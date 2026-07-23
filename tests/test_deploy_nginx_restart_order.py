"""Guard (2026-07-23): nginx/certbot must run BEFORE the restart, and must
never block or mask a successful restart.

Root cause: _post_pull_steps runs as a child process of the very
truesight-autopilot service `systemctl restart` targets. The restart used to
fire (step 3) before nginx/certbot (step 4) ran — so once the kill actually
landed a few seconds later, it could interrupt nginx/certbot mid-step and the
whole deploy would report status="error", even though the restart (the part
that actually matters — it's what loads the new code) had already succeeded.
A real deploy hit exactly this: reported "FAILED", but SSH into the box
afterward showed the process had, in fact, already restarted onto the new
commit.

Fix: nginx/certbot now runs first and is best-effort (its own failure is
recorded in `steps` but never raised/blocks); the restart is the last thing
that happens.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    from app.tools import deploy as dep
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app import unavailable: {exc}", allow_module_level=True)


def _wire_common(monkeypatch, calls: list[str]):
    monkeypatch.setattr(dep, "_get_current_commit", lambda remote_dir: "abc1234")
    monkeypatch.setattr(dep, "_write_deploy_marker", lambda commit, elapsed: None)

    class _FakePopen:
        def __init__(self, *a, **k):
            calls.append("restart_popen")

    monkeypatch.setattr(dep.subprocess, "Popen", _FakePopen)


def test_nginx_runs_before_restart_is_fired(monkeypatch):
    """Order of operations: nginx/certbot must complete before the restart
    Popen call, not after — that's the whole fix."""
    calls: list[str] = []
    _wire_common(monkeypatch, calls)

    def _fake_nginx(remote_dir, elevate):
        calls.append("nginx_certbot")

    monkeypatch.setattr(dep, "_run_local", lambda *a, **k: "")
    monkeypatch.setattr(dep, "_run_nginx_certbot", _fake_nginx)

    out = json.loads(dep._post_pull_steps("/opt/truesight_autopilot", 0.0, []))

    assert calls == ["nginx_certbot", "restart_popen"], calls
    assert out["status"] == "success"
    step_names = [s["step"] for s in out["steps"]]
    assert step_names.index("nginx_certbot") < step_names.index("restart_service")


def test_nginx_failure_does_not_block_or_mask_restart(monkeypatch):
    """A broken nginx config must not prevent the code-fix restart from
    firing, and must not turn the overall result into status=error."""
    calls: list[str] = []
    _wire_common(monkeypatch, calls)

    monkeypatch.setattr(dep, "_run_local", lambda *a, **k: "")

    def _boom(remote_dir, elevate):
        raise dep.DeployError("nginx -t failed: syntax error")

    monkeypatch.setattr(dep, "_run_nginx_certbot", _boom)

    out = json.loads(dep._post_pull_steps("/opt/truesight_autopilot", 0.0, []))

    assert "restart_popen" in calls, "restart must still fire despite nginx failure"
    assert out["status"] == "success"
    nginx_step = next(s for s in out["steps"] if s["step"] == "nginx_certbot")
    assert nginx_step["status"] == "error"
    assert "syntax error" in nginx_step["message"]
    restart_step = next(s for s in out["steps"] if s["step"] == "restart_service")
    assert restart_step["status"] == "ok"


def test_pip_install_failure_still_blocks_restart(monkeypatch):
    """Regression guard: unlike nginx, a pip install failure must remain
    fatal — a missing dependency can make the freshly-pulled code unsafe to
    run, so the restart (and nginx) must not proceed."""
    calls: list[str] = []
    _wire_common(monkeypatch, calls)

    def _fake_run_local(command, cwd=None, timeout=60):
        if "pip install" in command:
            raise dep.DeployError("pip install failed: could not build wheel")
        return ""

    monkeypatch.setattr(dep, "_run_local", _fake_run_local)
    monkeypatch.setattr(
        dep,
        "_run_nginx_certbot",
        lambda *a, **k: pytest.fail("nginx must not run if pip install failed"),
    )

    with pytest.raises(dep.DeployError, match="pip install failed"):
        dep._post_pull_steps("/opt/truesight_autopilot", 0.0, [])

    assert "restart_popen" not in calls
