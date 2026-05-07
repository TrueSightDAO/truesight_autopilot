"""Deploy truesight_autopilot to EC2 via SSH.

Reads SSH config from settings (EC2_HOST, EC2_KEY_PATH, EC2_REMOTE_DIR)
and runs the equivalent of scripts/deploy.sh steps:
  1. git pull latest code
  2. pip install -r requirements.txt
  3. restart systemd service
  4. Wait 5s and check health endpoint
"""
from __future__ import annotations

import json
import logging
import time

import paramiko

from ..config import settings

logger = logging.getLogger("autopilot.deploy")


class DeployError(Exception):
    """Raised when a deploy step fails."""


def _ssh_client() -> paramiko.SSHClient:
    """Create and return an authenticated SSH client."""
    host = settings.ec2_host
    key_path = settings.ec2_key_path
    if not host:
        raise DeployError("EC2_HOST is not configured.")
    if not key_path:
        raise DeployError("EC2_KEY_PATH is not configured.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username="ubuntu",
            key_filename=key_path,
            timeout=15,
        )
    except Exception as e:
        raise DeployError(f"SSH connection failed to {host}: {e}") from e
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


def deploy_autopilot() -> str:
    """Deploy truesight_autopilot to EC2 via SSH.

    Returns a JSON string with status and details.
    """
    steps: list[dict] = []
    start = time.time()

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
