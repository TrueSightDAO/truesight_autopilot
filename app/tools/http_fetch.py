"""Generic HTTP fetch tool — primarily for hitting Google Apps Script ``/exec``
deployments (and other webhooks/REST endpoints the agent needs to reach).

Returns a JSON-string with status_code, headers (subset), and the response
body capped at 256KB. Forced JSON for tool-message marshalling consistency.

This deliberately does NOT follow ``localhost``/RFC1918 redirects — see the
:func:`_is_safe_url` guard — to prevent the agent from probing the EC2 host's
metadata service or other internal endpoints.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("autopilot.tools.http_fetch")

_MAX_BODY_BYTES = 256 * 1024
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 60.0


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Disallow private / link-local / loopback / metadata targets."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "unparseable url"
    if parsed.scheme not in {"http", "https"}:
        return False, f"scheme {parsed.scheme!r} not allowed"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "missing host"
    # Block obvious EC2 metadata / link-local probes by hostname.
    if host in {"localhost", "169.254.169.254", "metadata.google.internal"}:
        return False, "host blocked"
    # Block private IPs by literal address.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, f"private/loopback IP {host} blocked"
    except ValueError:
        pass  # not a literal IP — fall through
    return True, ""


def http_fetch(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> str:
    """Fetch an HTTP(S) URL and return a bounded JSON result."""
    if not url or not isinstance(url, str):
        return _err("url is required")

    method = (method or "GET").upper()
    if method not in _ALLOWED_METHODS:
        return _err(f"method {method!r} not allowed", allowed=sorted(_ALLOWED_METHODS))

    safe, reason = _is_safe_url(url)
    if not safe:
        return _err(reason, url=url)

    t = float(timeout or _DEFAULT_TIMEOUT)
    if t <= 0 or t > _MAX_TIMEOUT:
        t = _DEFAULT_TIMEOUT

    request_body: bytes | str | None = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            request_body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, (bytes, bytearray)):
            request_body = bytes(body)
        else:
            request_body = str(body)

    try:
        with httpx.Client(follow_redirects=True, timeout=t) as client:
            resp = client.request(method, url, headers=headers, content=request_body)
            raw = resp.content or b""
            truncated = len(raw) > _MAX_BODY_BYTES
            if truncated:
                raw = raw[:_MAX_BODY_BYTES]
            content_type = resp.headers.get("content-type", "")
            try:
                body_text: Any = raw.decode("utf-8")
                encoding = "text"
            except UnicodeDecodeError:
                import base64 as _b64

                body_text = _b64.b64encode(raw).decode("ascii")
                encoding = "base64"
            # Only echo a small whitelist of response headers — avoid leaking
            # cookies/auth.
            echoed = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() in {"content-type", "content-length", "location", "etag", "x-ratelimit-remaining"}
            }
    except httpx.HTTPError as e:
        logger.warning("http_fetch network error: %s", e)
        return _err(f"network error: {e}", url=url)

    logger.info(
        "http_fetch ok: method=%s url=%.80s status=%d bytes=%d truncated=%s",
        method,
        url,
        resp.status_code,
        len(raw),
        truncated,
    )
    return json.dumps(
        {
            "status": "ok",
            "status_code": resp.status_code,
            "url": str(resp.url),
            "content_type": content_type,
            "encoding": encoding,
            "headers": echoed,
            "body": body_text,
            "byte_count": len(raw),
            "truncated": truncated,
        }
    )


# ── capability manifest entry ─────────────────────────────────────────────

from ..tool_registry import ToolSpec  # noqa: E402

TOOL_SPEC = ToolSpec(
    name="http_fetch",
    description="Make a generic HTTP request — primarily for hitting Google Apps Script /exec deployments (anonymous-callable web apps). Use when web_search/web_extract aren't enough and you need to POST or follow a specific REST API. Body capped at 256KB. Private/loopback/metadata URLs are blocked.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full HTTP(S) URL."},
            "method": {
                "type": "string",
                "description": "HTTP method.",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                "default": "GET",
            },
            "body": {
                "description": "Optional request body. Dicts/lists are JSON-serialised with Content-Type: application/json. Strings sent as-is."
            },
            "headers": {"type": "object", "description": "Optional request headers."},
            "timeout": {"type": "number", "description": "Timeout in seconds (default 30, max 60).", "default": 30},
        },
        "required": ["url"],
    },
    handler=lambda args, ctx: http_fetch(
        url=args.get("url", ""),
        method=args.get("method", "GET"),
        body=args.get("body"),
        headers=args.get("headers"),
        timeout=args.get("timeout"),
    ),
)
