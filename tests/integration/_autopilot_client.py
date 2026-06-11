"""Shared helper for autopilot integration tests.

Provides RSA signing using the dao_client governor key and the streaming
SSE consumer used by every test in this directory. Lives outside of the
autopilot codebase itself so the test surface mirrors what an external
client (DApp, dao_client CLI, third-party script) would do.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_AUTOPILOT_URL = os.environ.get("AUTOPILOT_URL", "http://127.0.0.1:8011")
DEFAULT_DAO_CLIENT_ENV = os.environ.get(
    "DAO_CLIENT_ENV",
    "/Users/garyjob/Applications/dao_client/.env",
)


def load_env(path: str | Path = DEFAULT_DAO_CLIENT_ENV) -> dict[str, str]:
    """Parse a dotenv-style file. Used to pick up the governor's RSA key
    from the dao_client config that signs Edgar contributions."""
    p = Path(path)
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v.strip().strip('"').strip("'")
    return out


class GovernorKey:
    """Wraps the RSA private key from dao_client/.env so tests can sign
    chat payloads the same way the real DApp does."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = env or load_env()
        if "PUBLIC_KEY" not in env or "PRIVATE_KEY" not in env:
            raise RuntimeError(
                "dao_client/.env missing PUBLIC_KEY/PRIVATE_KEY — run `python -m truesight_dao_client.auth login` first."
            )
        self.public_key_b64 = env["PUBLIC_KEY"]
        priv_der = base64.b64decode(env["PRIVATE_KEY"] + "===")
        self._priv = serialization.load_der_private_key(priv_der, password=None)

    @property
    def session_short(self) -> str:
        """First 16 chars of the public key — what DELETE /chat/active/{...} expects."""
        return self.public_key_b64[:16]

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Public-Key": self.public_key_b64, "Content-Type": "application/json"}

    def sign_payload(self, message: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "message": message,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nonce": uuid.uuid4().hex,
            **extra,
        }
        pj = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        sig = self._priv.sign(pj.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return {"payload": payload, "signature": base64.b64encode(sig).decode("ascii")}


async def stream_chat(
    key: GovernorKey,
    message: str,
    *,
    url: str = DEFAULT_AUTOPILOT_URL,
    on_event: Callable[[str, dict], None] | None = None,
    do_not_publish: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Open a /chat SSE stream and consume to completion.

    Calls `on_event(event_type, data)` for every parsed event so the test
    can assert on heartbeats, tool calls, queue interjections, etc.

    Returns a dict summary: { "events": [(type, data), ...], "saw_done": bool,
    "final_response": str|None }. Tests assert on the events list.

    `do_not_publish=True` by default so integration tests don't pollute
    the public truesight_autopilot_transcript repo.
    """
    body = key.sign_payload(message, do_not_publish=do_not_publish)
    events: list[tuple[str, dict]] = []
    saw_done = False
    final_response: str | None = None
    async with httpx.AsyncClient(timeout=timeout) as cl:
        async with cl.stream(
            "POST", f"{url}/chat", json=body, headers=key.headers
        ) as r:
            r.raise_for_status()
            async for raw in r.aiter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    data = json.loads(raw[5:].strip())
                except Exception:
                    continue
                t = data.get("type", "?")
                events.append((t, data))
                if on_event:
                    on_event(t, data)
                if t == "done":
                    saw_done = True
                    final_response = data.get("response", "")
                    break
                if t == "error":
                    break
    return {"events": events, "saw_done": saw_done, "final_response": final_response}


async def queue_message(
    key: GovernorKey, message: str, *, url: str = DEFAULT_AUTOPILOT_URL
) -> dict[str, Any]:
    """POST a follow-up message to /chat/queue (mid-stream interjection)."""
    body = key.sign_payload(message)
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.post(f"{url}/chat/queue", json=body, headers=key.headers)
        r.raise_for_status()
        return r.json()


async def cancel_chat(
    key: GovernorKey, *, url: str = DEFAULT_AUTOPILOT_URL
) -> dict[str, Any]:
    """DELETE /chat/active/{session_short} — abort the in-flight stream."""
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.delete(
            f"{url}/chat/active/{key.session_short}",
            headers={"X-Public-Key": key.public_key_b64},
        )
        return {"status_code": r.status_code, "body": r.text}


def banner(title: str) -> None:
    """Format a test header so output is scan-friendly."""
    print(f"\n{'─' * 70}\n  {title}\n{'─' * 70}", flush=True)


def report(name: str, ok: bool, detail: str = "") -> int:
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}{(' — ' + detail) if detail else ''}", flush=True)
    return 0 if ok else 1


def health_check(url: str = DEFAULT_AUTOPILOT_URL) -> bool:
    """Return True if the autopilot at `url` is responsive on /health."""
    import requests

    try:
        r = requests.get(f"{url}/health", timeout=4)
        return r.ok
    except Exception:
        return False


def require_running_autopilot(url: str = DEFAULT_AUTOPILOT_URL) -> None:
    """Print a clear error and exit non-zero if no autopilot is reachable."""
    if not health_check(url):
        print(f"  FAIL  No autopilot at {url}.", flush=True)
        print(
            "        Start one with:  python scripts/launch_local_autopilot.py  "
            "(or point AUTOPILOT_URL at a different instance).",
            flush=True,
        )
        sys.exit(2)
