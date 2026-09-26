"""Runtime host identity — where is Sophia running *right now*?

Prevents location hallucination: the system prompt embeds a live block stating
which EC2 instance (or local machine) the autopilot process is on, so Sophia
never has to guess "which box am I?" during a conversation.

Detected once via **IMDSv2** with a fast local-machine fallback (a single
~1.5 s timeout when not on EC2), then cached for the process lifetime. Uses
only the stdlib so it is import-safe everywhere (tests, Gary's Mac, CI).
"""

from __future__ import annotations

import functools
import platform
import socket
import urllib.request

_IMDS = "http://169.254.169.254/latest"
_TOKEN_TTL = "60"
_TIMEOUT = 1.5  # seconds — off-EC2 this gates one quick failure, then fallback

_META_FIELDS = {
    "instance_id": "instance-id",
    "instance_type": "instance-type",
    "availability_zone": "placement/availability-zone",
    "region": "placement/region",
    "public_ip": "public-ipv4",
    "private_ip": "local-ipv4",
    "ami_id": "ami-id",
}


def _imds_token() -> str | None:
    try:
        req = urllib.request.Request(
            f"{_IMDS}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": _TOKEN_TTL},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode().strip() or None
    except Exception:
        return None


def _imds_get(path: str, token: str) -> str | None:
    try:
        req = urllib.request.Request(
            f"{_IMDS}/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode().strip() or None
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def detect() -> dict:
    """Return a dict describing the current host. ``environment`` is 'ec2' or
    'local'. Cached — IMDS is probed at most once per process."""
    hostname = socket.gethostname()
    token = _imds_token()
    if not token:
        return {
            "environment": "local",
            "hostname": hostname,
            "platform": platform.platform(),
            "instance_id": None,
        }
    out = {"environment": "ec2", "hostname": hostname}
    for key, path in _META_FIELDS.items():
        out[key] = _imds_get(path, token)
    return out


def host_identity_block() -> str:
    """A markdown block for the system prompt stating where Sophia is running."""
    h = detect()
    if h.get("environment") == "ec2":
        return (
            "## WHERE YOU ARE RUNNING (live — detected via EC2 IMDS)\n"
            f"- Environment: **AWS EC2** (you are the autopilot box itself)\n"
            f"- Instance ID: {h.get('instance_id')}\n"
            f"- Instance type: {h.get('instance_type')}\n"
            f"- Region / AZ: {h.get('region')} / {h.get('availability_zone')}\n"
            f"- Public IP: {h.get('public_ip')} · Private IP: {h.get('private_ip')}\n"
            f"- AMI: {h.get('ami_id')} · Hostname: {h.get('hostname')}\n"
            "To run a shell or **sudo** command on THIS box, call "
            "`ssh_run(host='autopilot', ...)` — it loops back to 127.0.0.1 on "
            "your own host. The `ubuntu` user has passwordless sudo, so you can "
            "install packages (e.g. `sudo apt-get install -y tesseract-ocr`) "
            "yourself. Never tell a governor you lack shell or sudo access on "
            "your own box — you have both.\n"
        )
    return (
        "## WHERE YOU ARE RUNNING (live)\n"
        f"- Environment: **local / non-EC2 machine** (not the production autopilot)\n"
        f"- Hostname: {h.get('hostname')}\n"
        f"- Platform: {h.get('platform')}\n"
        "The `ssh_run` fleet tools target remote production hosts from here.\n"
    )
