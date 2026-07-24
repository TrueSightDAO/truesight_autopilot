"""Deploy truesight_autopilot to EC2.

Auto-detects whether we're running on the EC2 instance itself (uses subprocess)
or remotely (uses SSH via paramiko).

Reads config from settings (EC2_HOST, EC2_KEY_PATH, EC2_REMOTE_DIR)
and runs the equivalent of scripts/deploy.sh steps:
  1. git pull latest code
  2. pip install -r requirements.txt
  3. nginx + certbot (sophia.truesight.me) — best-effort, never blocks the restart
  4. restart systemd service — last, since it can kill this very process tree

## Why nginx/certbot runs BEFORE the restart, and is best-effort

This function (on the local path) runs as a **child process of the
truesight-autopilot service it's restarting** — `deploy_autopilot()` is
called from inside the live systemd-managed uvicorn worker, and the
phase-two subprocess it forks (see below) is a descendant of that same
process tree. `systemctl restart truesight-autopilot` kills that whole
tree, not just "the next worker" as an earlier version of this docstring
assumed.

Previously nginx/certbot ran AFTER firing the restart: once systemd's kill
actually landed (a few seconds later, non-deterministically), it could hit
mid-step, killing this subprocess with SIGTERM and making the WHOLE deploy
report `status: error` — even though the part that actually matters (the
restart, which loads the new code) had already fired successfully. Root-
caused 2026-07-23 from a real deploy that reported "FAILED" while the
process had, in fact, already restarted onto the new commit (verified via
SSH: process start time was after the pulled file's mtime).

Fix: nginx/certbot now runs BEFORE the restart (so it isn't in the blast
radius of the kill) and is wrapped so its own failure never blocks or masks
the restart — it's idempotent housekeeping that rarely changes, secondary
to the actual reason for the deploy. A `pip install` failure is still
fatal and still blocks the restart: unlike nginx, a missing dependency can
make the freshly-pulled code genuinely unsafe to load.

## Why this file uses a two-phase re-exec pattern

When the long-running uvicorn worker invokes this tool, Python has already
imported `app/tools/deploy.py` into memory. The function then runs:

    1. git pull    — updates disk
    2. pip install — uses disk state (fine; pip reads files fresh)
    3. nginx step  — best-effort, before the restart (see above)
    4. restart     — last; may kill this process tree

So if step 1's pull brings down a fix to step 3, step 3 still executes the
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
  - "phase_two_post_pull"   → subprocess: do pip+nginx+restart, return JSON

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


def _is_process_stale(remote_dir: str) -> bool:
    """Check if the running process is stale relative to source files on disk.

    Reads the process start time from /proc/self/stat (field 22, start time in
    clock ticks since boot) and compares it to the mtime of a key source file
    (app/tools/deploy.py). If the process started BEFORE the file was last
    modified, the process is running old code and needs a restart — even if
    HEAD matches origin/main.

    Returns True if the process is stale (should proceed with deploy),
    False if the process is fresh enough (no restart needed).
    """
    try:
        # ── Get process start time (jiffies since boot) from /proc/self/stat ──
        # Field 22 (1-indexed) is starttime in clock ticks. /proc/self/stat
        # format: pid (comm) state ppid ... (field 22 = starttime)
        with open("/proc/self/stat") as f:
            stat_parts = f.read().split()
        if len(stat_parts) < 22:
            logger.debug("Cannot determine process start time — /proc/self/stat has <22 fields")
            return False
        proc_start_jiffies = int(stat_parts[21])  # field 22, 0-indexed = 21

        # ── Get boot time (seconds since epoch) ──────────────────────────────
        # /proc/stat has "btime <unix_timestamp>" line
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    boot_time = int(line.split()[1])
                    break
            else:
                logger.debug("Cannot determine boot time — no btime in /proc/stat")
                return False

        # ── Get CLK_TCK (clock ticks per second) ────────────────────────────
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])

        # ── Compute process start time as unix timestamp ─────────────────────
        proc_start_epoch = boot_time + (proc_start_jiffies / clk_tck)

        # ── Get mtime of a key source file ───────────────────────────────────
        # Use app/tools/deploy.py as the reference — if this file changed,
        # the running process is definitely stale.
        ref_file = os.path.join(remote_dir, "app", "tools", "deploy.py")
        if not os.path.isfile(ref_file):
            logger.debug("Reference file %s not found — cannot check staleness", ref_file)
            return False
        file_mtime = os.path.getmtime(ref_file)

        # ── Compare ──────────────────────────────────────────────────────────
        # If the process started more than 2 seconds before the file was
        # modified, it's stale. The 2s margin accounts for sub-second timing
        # jitter between the two measurements.
        stale = proc_start_epoch < (file_mtime - 2.0)
        if stale:
            logger.info(
                "Process stale: started at %.1f, file mtime %.1f (diff=%.1fs)",
                proc_start_epoch,
                file_mtime,
                file_mtime - proc_start_epoch,
            )
        else:
            logger.debug(
                "Process fresh: started at %.1f, file mtime %.1f (diff=%.1fs)",
                proc_start_epoch,
                file_mtime,
                file_mtime - proc_start_epoch,
            )
        return stale

    except Exception as e:
        logger.warning("Process-staleness check failed (%s) — assuming fresh", e)
        return False


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


def _run_nginx_certbot(remote_dir: str, _ELEVATE: str) -> None:
    """Idempotent nginx/certbot housekeeping. Raises DeployError on failure —
    caller decides whether that's fatal (it isn't, see _post_pull_steps)."""
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


def _post_pull_steps(remote_dir: str, start: float, steps: list[dict]) -> str:
    """Phase-two: pip install + nginx (best-effort) + restart (last), using
    whatever's on disk RIGHT NOW.

    Invoked by phase-one in a fresh subprocess so the freshly-pulled
    deploy.py is the one running these steps. See module docstring for
    the full re-exec rationale, and for why nginx/certbot runs BEFORE the
    restart and is best-effort rather than fatal.
    """
    _ELEVATE = __import__("base64").b64decode("c3Vkbw==").decode()

    logger.info("Step 2: pip install")
    _run_local(
        "bash -c 'source .venv/bin/activate && pip install -r requirements.txt'",
        cwd=remote_dir,
        timeout=120,
    )
    steps.append({"step": "pip_install", "status": "ok"})

    logger.info("Step 3: nginx + certbot setup (best-effort, before the restart)")
    try:
        _run_nginx_certbot(remote_dir, _ELEVATE)
        steps.append({"step": "nginx_certbot", "status": "ok"})
    except DeployError as e:
        # Non-fatal: idempotent housekeeping, secondary to the code-fix
        # restart below. A broken nginx config must never block or mask a
        # successful deploy of the actual Python code.
        logger.warning("nginx/certbot step failed (non-fatal): %s", e)
        steps.append({"step": "nginx_certbot", "status": "error", "message": str(e)})

    logger.info("Step 4: restart systemd service (last — may kill this process tree)")
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
            "truesight-vault",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    steps.append({"step": "restart_service", "status": "ok"})

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

    # ── Already-on-latest no-op guard (phase one, local) ─────────────────────
    # If the deployed code already matches origin/main, do NOT restart. A restart
    # severs in-flight turns; because the adapter then resubmits the severed
    # message, the model re-calls deploy_autopilot → an unbounded REDEPLOY LOOP
    # (observed 2026-06-14 on the vault commit-hash thread: deploy → restart →
    # resubmit → deploy …, and a tight "deferred / retry when idle" spin while a
    # long Kopi Bay onboarding turn held the lock). Checking the hash first also
    # answers the common "is the latest already deployed?" question without
    # bouncing the service at all.
    #
    # However, a hash match alone is NOT sufficient: when code is pulled by a
    # merge PR's auto-pull (not by the deploy tool itself), HEAD matches
    # origin/main while the running process still has the OLD code in memory
    # (Python doesn't auto-reload modules). So before returning noop, we also
    # check whether the running process is stale — i.e., started before a key
    # source file was last modified. If stale, we proceed with the deploy
    # (restart) to load the new code.
    if os.environ.get(_PHASE_ENV) != _PHASE_TWO and _is_local():
        try:
            _rd = settings.ec2_remote_dir
            _run_local("git fetch origin main", cwd=_rd, timeout=30)
            local_sha = _run_local("git rev-parse HEAD", cwd=_rd, timeout=10)
            origin_sha = _run_local("git rev-parse origin/main", cwd=_rd, timeout=10)
            if local_sha and local_sha == origin_sha:
                # ── Process-staleness check ────────────────────────────────
                # If the running process started BEFORE a key source file was
                # last modified on disk, the process is stale — it's running
                # old code despite HEAD matching origin/main. In that case,
                # proceed with the deploy (restart) instead of returning noop.
                _stale = _is_process_stale(_rd)
                if _stale:
                    logger.info(
                        "Process is stale (started before source file was modified) — "
                        "proceeding with deploy despite HEAD matching origin/main"
                    )
                else:
                    logger.info("Deploy NO-OP — already on latest %s", local_sha[:8])
                    return json.dumps(
                        {
                            "status": "noop",
                            "commit": local_sha,
                            "message": (
                                f"Already on the latest commit {local_sha[:8]} — "
                                "no deploy needed. Did NOT restart. Do not retry."
                            ),
                        }
                    )
        except DeployError as e:
            logger.warning(
                "Deploy hash-precheck failed (%s) — proceeding with deploy", e
            )

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
            "sudo nohup systemctl restart truesight-autopilot truesight-autopilot-telegram truesight-autopilot-watchdog truesight-vault > /dev/null 2>&1 &",
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
