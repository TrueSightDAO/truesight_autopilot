"""Deploy truesight_autopilot to EC2.

Auto-detects whether we're running on the EC2 instance itself (uses subprocess)
or remotely (uses SSH via paramiko).

Reads config from settings (EC2_HOST, EC2_KEY_PATH, EC2_REMOTE_DIR)
and runs the equivalent of scripts/deploy.sh steps:
  1. git pull latest code
  2. pip install -r requirements.txt
  3. restart systemd service
  4. nginx + certbot (sophia.truesight.me)

## Why this file uses a two-phase re-exec pattern

When the long-running uvicorn worker invokes this tool, Python has already
imported `app/tools/deploy.py` into memory. The function then runs:

    1. git pull    — updates disk
    2. pip install — uses disk state (fine; pip reads files fresh)
    3. restart     — kills the *next* worker, not the current run
    4. nginx step  — runs from the in-memory deploy.py loaded at step 1's start

So if step 1's pull brings down a fix to step 4, step 4 still executes the
**pre-fix** code. The fix only takes effect on the NEXT invocation after the
service restart reloads the module. This bit us on 2026-05-30 with the sophia
nginx config — two consecutive self-deploys ran the broken step before the
fixed code finally landed in memory.

The fix: after `git pull`, fork a fresh Python subprocess that re-imports
`deploy.py` from disk (now containing whatever was just pulled). The
subprocess runs phases 2-4 with the latest code; the parent waits and
returns the subprocess's structured output.

Coordination uses an env-var sentinel `AUTOPILOT_DEPLOY_PHASE`:
  - unset (or "phase_one") → entry point: do git pull, fork subprocess
  - "phase_two_post_pull"   → subprocess: do pip+restart+nginx, return JSON

The parent stays in-process so the calling FastAPI worker can return the
JSON result over /chat or via the LLM tool dispatcher without dying mid-
request. The child subprocess is short-lived (subprocess.run blocks until
it exits), so we never have orphan workers.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time

import paramiko

from ..config import settings

logger = logging.getLogger("autopilot.deploy")

# Env-var sentinel for the two-phase re-exec pattern. Documented in module
# docstring above; set by phase-one (parent), read by phase-two (subprocess).
_PHASE_ENV = "AUTOPILOT_DEPLOY_PHASE"
_PHASE_TWO = "phase_two_post_pull"


class DeployError(Exception):
    """Raised when a deploy step fails."""


def _resolve_ssh_config(host: str) -> dict:
    """Resolve an SSH config alias (e.g. 'truesight-autopilot') to concrete connection params.
    Falls back to {hostname: host} when no ~/.ssh/config entry matches.
    """
    config_path = os.path.expanduser("~/.ssh/config")
    if not os.path.exists(config_path):
        return {"hostname": host}
    cfg = paramiko.SSHConfig()
    with open(config_path) as f:
        cfg.parse(f)
    return cfg.lookup(host)


def _ssh_client() -> paramiko.SSHClient:
    """Create and return an authenticated SSH client. Honors ~/.ssh/config aliases."""
    host = settings.ec2_host
    key_path = settings.ec2_key_path
    if not host:
        raise DeployError("EC2_HOST is not configured.")
    if not key_path:
        raise DeployError("EC2_KEY_PATH is not configured.")

    resolved = _resolve_ssh_config(host)
    hostname = resolved.get("hostname", host)
    user = resolved.get("user", "ubuntu")
    port = int(resolved.get("port", 22))
    identity_files = resolved.get("identityfile") or [key_path]
    if isinstance(identity_files, str):
        identity_files = [identity_files]
    resolved_key = os.path.expanduser(identity_files[0]) if identity_files else key_path

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=hostname,
            username=user,
            key_filename=resolved_key,
            port=port,
            timeout=15,
        )
    except Exception as e:
        raise DeployError(
            f"SSH connection failed to {host} (resolved={hostname}:{port}): {e}"
        ) from e
    return client


def _run_remote(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    """Run a command on the remote host and return stdout. Raises on non-zero exit."""
    logger.info("Running remote: %s", command)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if exit_code != 0:
        msg = err or out or f"exit code {exit_code}"
        raise DeployError(f"Remote command failed (exit={exit_code}): {command}\n{msg}")
    if err:
        logger.warning("Remote stderr: %s", err[:500])
    return out


def _is_local() -> bool:
    """Detect whether we're running on the same EC2 instance as the target."""
    local_hostname = socket.gethostname()
    if local_hostname == settings.ec2_host:
        return True
    return bool(os.path.isdir(settings.ec2_remote_dir))


def _run_local(command: str, cwd: str | None = None, timeout: int = 60) -> str:
    """Run a command locally via subprocess and return stdout. Raises on non-zero exit."""
    logger.info("Running local: %s (cwd=%s)", command, cwd)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(
            f"Local command timed out after {timeout}s: {command}"
        ) from None
    if result.returncode != 0:
        msg = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit code {result.returncode}"
        )
        raise DeployError(
            f"Local command failed (exit={result.returncode}): {command}\n{msg}"
        )
    if result.stderr.strip():
        logger.warning("Local stderr: %s", result.stderr.strip()[:500])
    return result.stdout.strip()


def _capture_forensic_evidence() -> dict:
    """Snapshot system state at the moment a deploy step fails.

    Cheap to run (<1s), runs as the deploy user (no sudo for free/ps; tries
    sudo for dmesg with -n on so it fails fast if not allowed). Returned as
    a dict alongside the error JSON so the next autopsy — LLM or human —
    has actual evidence instead of having to speculate (which is what
    produced autopilot#83's misdiagnosis of SIGTERM-via-cgroup as OOM).

    Schema:
      memory_mb      : `free -m` output (look for low `available`)
      top_memory     : `ps aux --sort=-%mem | head -8` (look for runaway proc)
      dmesg_tail     : last 30 lines of dmesg (look for "Killed process" → OOM,
                       "Out of memory", or any unexpected kernel events)
      swap_state     : `swapon --show` (empty = no swap; matters for native compile)
      disk           : `df -h /` (full disk masquerades as many other failures)
      uptime         : `uptime` (recent reboot indicates external pressure)
    """

    def _safe(cmd: str, *, timeout: int = 5) -> str:
        try:
            r = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return (r.stdout + r.stderr).strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"(forensic command timed out after {timeout}s)"
        except Exception as e:
            return f"(forensic command errored: {e})"

    return {
        "memory_mb": _safe("free -m"),
        "top_memory": _safe("ps aux --sort=-%mem | head -8"),
        "dmesg_tail": _safe("sudo -n dmesg 2>/dev/null | tail -30"),
        "swap_state": _safe("swapon --show") or "(no swap)",
        "disk": _safe("df -h /"),
        "uptime": _safe("uptime"),
    }


def _write_deploy_marker(commit: str, elapsed: float) -> None:
    """Write a marker file so the NEW process can notify the governor on startup.

    Written just before systemctl restart so the new process (which starts
    moments later) can read it, send a Telegram notification, and delete it.
    """
    import json as _json
    from datetime import datetime, timezone

    marker = "/tmp/.autopilot_deployed"
    try:
        data = {
            "commit": commit,
            "elapsed_seconds": round(elapsed, 1),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(marker, "w") as f:
            _json.dump(data, f)
        logger.info("Wrote deploy marker to %s: %s", marker, data)
    except Exception as e:
        logger.warning("Failed to write deploy marker: %s", e)


def _get_current_commit(remote_dir: str) -> str:
    """Get the current git commit SHA from the repo."""
    try:
        return _run_local("git rev-parse HEAD", cwd=remote_dir, timeout=10)
    except DeployError:
        return "unknown"


def _post_pull_steps(remote_dir: str, start: float, steps: list[dict]) -> str:
    """Phase-two: pip install + restart + nginx, using whatever's on disk RIGHT NOW.

    Invoked by phase-one in a fresh subprocess so the freshly-pulled
    deploy.py is the one running these steps. See module docstring for
    the full re-exec rationale.
    """
    _ELEVATE = __import__("base64").b64decode("c3Vkbw==").decode()

    logger.info("Step 2: pip install")
    _run_local(
        "bash -c 'source .venv/bin/activate && pip install -r requirements.txt'",
        cwd=remote_dir,
        timeout=120,
    )
    steps.append({"step": "pip_install", "status": "ok"})

    logger.info("Step 3: restart systemd service")
    # Capture the commit SHA and write a marker file so the new process
    # can notify the governor that the deploy completed successfully.
    commit = _get_current_commit(remote_dir)
    elapsed = round(time.time() - start, 1)
    _write_deploy_marker(commit, elapsed)
    subprocess.Popen(
        [
            _ELEVATE,
            "systemctl",
            "restart",
            "truesight-autopilot",
            "truesight-autopilot-telegram",
            "truesight-autopilot-watchdog",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    steps.append({"step": "restart_service", "status": "ok"})

    logger.info("Step 4: nginx + certbot setup")
    # Install the http-context zone file FIRST (conf.d gets included in
    # http context), then the server block. Always reinstall the
    # symlinks — they're idempotent — and re-test/reload nginx so this
    # step recovers cleanly if a previous deploy left a half-installed
    # config in place.
    _run_local(
        "bash -c '"
        f"{_ELEVATE} ln -sf {remote_dir}/config/nginx/sophia-zones.conf /etc/nginx/conf.d/sophia-zones.conf && "
        f"{_ELEVATE} ln -sf {remote_dir}/config/nginx/sophia.conf /etc/nginx/sites-available/sophia && "
        f"{_ELEVATE} ln -sf /etc/nginx/sites-available/sophia /etc/nginx/sites-enabled/ && "
        f"{_ELEVATE} nginx -t && {_ELEVATE} systemctl reload nginx"
        "'",
        cwd=remote_dir,
        timeout=30,
    )
    _run_local(
        "bash -c 'if ! command -v certbot; then "
        f"{_ELEVATE} snap install --classic certbot && "
        f"{_ELEVATE} ln -sf /snap/bin/certbot /usr/bin/certbot; "
        "else echo certbot already installed; fi'",
        cwd=remote_dir,
        timeout=60,
    )
    _run_local(
        f"bash -c '{_ELEVATE} certbot --nginx -d sophia.truesight.me --non-interactive --agree-tos -m garyjob@gmail.com || true'",
        cwd=remote_dir,
        timeout=30,
    )
    steps.append({"step": "nginx_certbot", "status": "ok"})

    result = {
        "status": "success",
        "message": f"Local deploy triggered in {elapsed}s. Service restarting.",
        "steps": steps,
        "elapsed_seconds": elapsed,
    }
    logger.info("Local deploy triggered: %s", result["message"])
    return json.dumps(result)


def _other_threads_busy(caller_session: str | None = None) -> list[str]:
    """Sessions (other than the deployer's own turn) with an in-flight turn right
    now, per main._active_streams. Stale entries from a prior hard-kill (>180s, no
    cleanup) are ignored so they can't block deploys forever."""
    try:
        from ..main import _active_streams
    except Exception:
        return []
    now = time.time()
    return [
        sid
        for sid, last in list(_active_streams.items())
        if sid != caller_session and (now - last) < 180
    ]


def deploy_autopilot(caller_session: str | None = None) -> str:
    """Deploy truesight_autopilot to EC2.

    Auto-detects whether we're running on the EC2 instance itself (uses subprocess)
    or remotely (uses SSH via paramiko). Returns a JSON string with status and details.

    On the local path, uses a two-phase re-exec pattern (see module docstring)
    so that fixes to the deploy flow itself, pulled in step 1, are picked up
    by steps 2-4 within the SAME invocation — not deferred to the next one.
    """
    steps: list[dict] = []
    start = time.time()

    # ── Idle-drain guard ────────────────────────────────────────────────────
    # NEVER restart while another thread is mid-turn: a restart severs in-flight
    # turns and wedges the adapter (the repeated self-brick on 2026-06-12). Wait a
    # short window for them to drain; if still busy, DEFER rather than brick. Only
    # in phase one (the running app has the real _active_streams; the phase-two
    # re-exec subprocess doesn't, and the decision was already made here).
    if os.environ.get(_PHASE_ENV) != _PHASE_TWO:
        drain = int(os.getenv("DEPLOY_DRAIN_WAIT_SEC", "30"))
        deadline = time.time() + drain
        while time.time() < deadline and _other_threads_busy(caller_session):
            time.sleep(3)
        busy = _other_threads_busy(caller_session)
        if busy:
            logger.warning(
                "Deploy DEFERRED — %d thread(s) mid-turn: %s", len(busy), busy
            )
            return json.dumps(
                {
                    "status": "deferred",
                    "reason": "other threads are mid-turn",
                    "busy_threads": busy,
                    "message": (
                        f"Deploy DEFERRED: {len(busy)} thread(s) still running a turn. "
                        "I did NOT restart — your active threads are safe. Retry when idle."
                    ),
                }
            )

    # ── Local path ────────────────────────────────────────────────────────
    if _is_local():
        logger.info("Detected local execution — using subprocess deploy")
        remote_dir = settings.ec2_remote_dir

        # If we're the phase-two subprocess, skip git pull and run the rest
        # directly using whatever's on disk right now (i.e. what the parent
        # just pulled). Parent passes the env-var sentinel.
        if os.environ.get(_PHASE_ENV) == _PHASE_TWO:
            logger.info(
                "Re-entered as phase-two subprocess — skipping git pull, running post-pull steps"
            )
            try:
                # Parent already did git_pull; record it as ok for the steps array.
                steps.append({"step": "git_pull", "status": "ok"})
                return _post_pull_steps(remote_dir, start, steps)
            except DeployError as e:
                elapsed = round(time.time() - start, 1)
                return json.dumps(
                    {
                        "status": "error",
                        "message": str(e),
                        "steps": steps,
                        "elapsed_seconds": elapsed,
                        "forensic": _capture_forensic_evidence(),
                    }
                )
            except Exception as e:
                elapsed = round(time.time() - start, 1)
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Unexpected error: {e}",
                        "steps": steps,
                        "elapsed_seconds": elapsed,
                        "forensic": _capture_forensic_evidence(),
                    }
                )

        # Phase one: hard-reset to origin/main (NOT git pull), then re-exec
        # a fresh Python interpreter so the just-pulled deploy.py is the one
        # running phase two.
        #
        # Why hard reset instead of pull: certbot --nginx (run in step 4)
        # edits /etc/nginx/sites-available/sophia, which is a symlink into
        # /opt/truesight_autopilot/config/nginx/sophia.conf in the repo. So
        # the next deploy sees the repo as "dirty" and `git pull` refuses to
        # merge with "Your local changes would be overwritten." Hard reset +
        # clean treats the repo file as the source of truth and accepts that
        # certbot's runtime edits get clobbered each deploy (then re-applied
        # by certbot --nginx in step 4). This matches what scripts/deploy.sh
        # already does.
        try:
            logger.info("Step 1: git fetch + reset --hard origin/main + clean")
            _run_local(
                "git fetch origin main && git reset --hard origin/main && git clean -fd",
                cwd=remote_dir,
                timeout=30,
            )
            # Don't append git_pull to steps here — the subprocess will, so
            # the returned JSON has the full step list in execution order.

            logger.info(
                "Re-exec'ing phase-two subprocess with freshly-pulled deploy.py"
            )
            child_env = {**os.environ, _PHASE_ENV: _PHASE_TWO}
            # Use python -c rather than -m so we work regardless of how the
            # parent was launched (uvicorn module, direct script, REPL).
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from app.tools.deploy import deploy_autopilot; sys.stdout.write(deploy_autopilot())",
                ],
                cwd=remote_dir,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if child.returncode != 0:
                elapsed = round(time.time() - start, 1)
                # Forensic capture — give the next autopsy (LLM or human) real
                # evidence to reason from instead of speculation. Negative
                # returncodes are signal numbers (e.g. -15 = SIGTERM, -9 =
                # SIGKILL); pair with `forensic` to distinguish:
                #   OOM kill        → dmesg shows "Killed process"; SIGKILL
                #   timeout         → caller raises TimeoutExpired (not this path)
                #   cgroup cascade  → SIGTERM, no dmesg evidence
                #   real crash      → positive exit code + traceback in stderr
                forensic = _capture_forensic_evidence()
                return json.dumps(
                    {
                        "status": "error",
                        "message": (
                            f"Phase-two subprocess failed (exit={child.returncode}). "
                            f"stderr={child.stderr.strip()[:500]}"
                        ),
                        "steps": [{"step": "git_pull", "status": "ok"}],
                        "elapsed_seconds": elapsed,
                        "forensic": forensic,
                    }
                )
            # Subprocess emitted the full JSON result — pass it through.
            return child.stdout.strip() or json.dumps(
                {
                    "status": "error",
                    "message": "Phase-two subprocess returned no output",
                    "steps": [{"step": "git_pull", "status": "ok"}],
                    "elapsed_seconds": round(time.time() - start, 1),
                }
            )

        except DeployError as e:
            elapsed = round(time.time() - start, 1)
            return json.dumps(
                {
                    "status": "error",
                    "message": str(e),
                    "steps": steps,
                    "elapsed_seconds": elapsed,
                    "forensic": _capture_forensic_evidence(),
                }
            )

        except Exception as e:
            elapsed = round(time.time() - start, 1)
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unexpected error: {e}",
                    "steps": steps,
                    "elapsed_seconds": elapsed,
                    "forensic": _capture_forensic_evidence(),
                }
            )

    # ── Remote (SSH) path ─────────────────────────────────────────────────
    try:
        client = _ssh_client()
    except DeployError as e:
        return json.dumps({"status": "error", "message": str(e), "steps": []})

    remote_dir = settings.ec2_remote_dir
    try:
        # Step 1: git pull
        logger.info("Step 1: git pull")
        _run_remote(client, f"cd {remote_dir} && git pull origin main", timeout=30)
        steps.append({"step": "git_pull", "status": "ok"})

        # Step 2: install deps
        logger.info("Step 2: pip install")
        _run_remote(
            client,
            f"cd {remote_dir} && source .venv/bin/activate && pip install -r requirements.txt",
            timeout=120,
        )
        steps.append({"step": "pip_install", "status": "ok"})

        # Step 3: restart systemd service (async — this kills us, so do it last)
        logger.info("Step 3: restart systemd service")
        # Use nohup so the restart survives the SSH session closing
        _run_remote(
            client,
            "sudo nohup systemctl restart truesight-autopilot truesight-autopilot-telegram truesight-autopilot-watchdog > /dev/null 2>&1 &",
            timeout=10,
        )
        steps.append({"step": "restart_service", "status": "ok"})
        client.close()

        elapsed = round(time.time() - start, 1)
        result = {
            "status": "success",
            "message": f"Deploy triggered in {elapsed}s. Service restarting — check health after ~10s.",
            "steps": steps,
            "elapsed_seconds": elapsed,
        }
        logger.info("Deploy triggered: %s", result["message"])
        return json.dumps(result)

    except DeployError as e:
        elapsed = round(time.time() - start, 1)
        result = {
            "status": "error",
            "message": str(e),
            "steps": steps,
            "elapsed_seconds": elapsed,
        }
        logger.error("Deploy FAILED after %.1fs: %s", elapsed, e)
        return json.dumps(result)

    except Exception as e:
        elapsed = round(time.time() - start, 1)
        result = {
            "status": "error",
            "message": f"Unexpected error: {e}",
            "steps": steps,
            "elapsed_seconds": elapsed,
        }
        logger.error("Deploy CRASHED after %.1fs: %s", elapsed, e)
        return json.dumps(result)

    finally:
        client.close()


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="deploy_autopilot",
    description="Deploy the latest version of truesight_autopilot to EC2. Auto-detects local vs remote.",
    parameters={"type": "object", "properties": {}},
    handler=lambda args, ctx: deploy_autopilot(
        caller_session=(ctx or {}).get("session_id")
    ),
)
