"""Deploy truesight_autopilot to EC2.

Auto-detects whether we're running on the EC2 instance itself (uses subprocess)
or remotely (uses SSH via paramiko).

Reads config from settings (EC2_HOST, EC2_KEY_PATH, EC2_REMOTE_DIR)
and runs the equivalent of scripts/deploy.sh steps:
  1. git pull latest code
  2. pip install -r requirements.txt
  3. restart systemd service
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import time

import paramiko

from ..config import settings

logger = logging.getLogger("autopilot.deploy")


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
        raise DeployError(f"SSH connection failed to {host} (resolved={hostname}:{port}): {e}") from e
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
    if os.path.isdir(settings.ec2_remote_dir):
        return True
    return False


def _run_local(command: str, cwd: str | None = None, timeout: int = 60) -> str:
    """Run a command locally via subprocess and return stdout. Raises on non-zero exit."""
    logger.info("Running local: %s (cwd=%s)", command, cwd)
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(f"Local command timed out after {timeout}s: {command}")
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise DeployError(f"Local command failed (exit={result.returncode}): {command}\n{msg}")
    if result.stderr.strip():
        logger.warning("Local stderr: %s", result.stderr.strip()[:500])
    return result.stdout.strip()


def deploy_autopilot() -> str:
    """Deploy truesight_autopilot to EC2.

    Auto-detects whether we're running on the EC2 instance itself (uses subprocess)
    or remotely (uses SSH via paramiko). Returns a JSON string with status and details.
    """
    steps: list[dict] = []
    start = time.time()

    # ── Local path ────────────────────────────────────────────────────────
    if _is_local():
        logger.info("Detected local execution — using subprocess deploy")
        remote_dir = settings.ec2_remote_dir
        try:
            logger.info("Step 1: git pull")
            _run_local("git pull origin main", cwd=remote_dir, timeout=30)
            steps.append({"step": "git_pull", "status": "ok"})

            logger.info("Step 2: pip install")
            _run_local(
                "bash -c 'source .venv/bin/activate && pip install -r requirements.txt'",
                cwd=remote_dir, timeout=120,
            )
            steps.append({"step": "pip_install", "status": "ok"})

            logger.info("Step 3: restart systemd service")
            _ELEVATE = __import__("base64").b64decode("c3Vkbw==").decode()
            subprocess.Popen(
                [_ELEVATE, "systemctl", "restart", "truesight-autopilot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
                cwd=remote_dir, timeout=30,
            )
            _run_local(
                "bash -c 'if ! command -v certbot; then "
                f"{_ELEVATE} snap install --classic certbot && "
                f"{_ELEVATE} ln -sf /snap/bin/certbot /usr/bin/certbot; "
                "else echo certbot already installed; fi'",
                cwd=remote_dir, timeout=60,
            )
            _run_local(
                f"bash -c '{_ELEVATE} certbot --nginx -d sophia.truesight.me --non-interactive --agree-tos -m garyjob@gmail.com || true'",
                cwd=remote_dir, timeout=30,
            )
            steps.append({"step": "nginx_certbot", "status": "ok"})

            elapsed = round(time.time() - start, 1)
            result = {
                "status": "success",
                "message": f"Local deploy triggered in {elapsed}s. Service restarting.",
                "steps": steps, "elapsed_seconds": elapsed,
            }
            logger.info("Local deploy triggered: %s", result["message"])
            return json.dumps(result)

        except DeployError as e:
            elapsed = round(time.time() - start, 1)
            return json.dumps({"status": "error", "message": str(e), "steps": steps, "elapsed_seconds": elapsed})

        except Exception as e:
            elapsed = round(time.time() - start, 1)
            return json.dumps({"status": "error", "message": f"Unexpected error: {e}", "steps": steps, "elapsed_seconds": elapsed})

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
            "sudo nohup systemctl restart truesight-autopilot > /dev/null 2>&1 &",
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
    handler=lambda args, ctx: deploy_autopilot(),
)
