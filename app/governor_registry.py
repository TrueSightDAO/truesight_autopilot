"""Governor registry loader — reads dao_members.json from treasury-cache (GitHub raw).

Supports two lookup modes:
1. Point-lookup via resolve_key() — fetches public_keys/<sha256>.json (fast, ~1 KB)
2. Enumeration via load_governors() — fetches dao_members.json (full monolith, ~129 KB)

The point-lookup is preferred for single-key checks (sign-in, vault access).
The monolith is retained for enumeration (policy.py binding check) and as a
fallback if a per-key file is absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_DEFAULT_MEMBERS_URL = "https://raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/dao_members.json"
_DEFAULT_PUBLIC_KEYS_BASE = "https://raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys"
_CACHE_TTL_SECONDS = int(os.getenv("GOVERNORS_CACHE_TTL", "300"))

# Per-key cache: sha256 -> {identity, fetched_at}
# Shorter TTL than the monolith cache because per-key fetches are cheap (~1 KB).
_PER_KEY_CACHE_TTL = int(os.getenv("PUBLIC_KEY_CACHE_TTL", "60"))

_cache: dict[str, any] = {
    "data": None,
    "fetched_at": 0.0,
    "url": None,
}

# Per-key cache: sha256 -> {data, fetched_at}
_per_key_cache: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def _sha256(public_key_b64: str) -> str:
    """Compute SHA-256 hex digest of a base64 public key."""
    return hashlib.sha256(public_key_b64.encode("utf-8")).hexdigest()


def _load_local(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _fetch_remote(url: str) -> dict:
    resp = httpx.get(url, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _fetch_github_contents_api(path: str) -> dict | None:
    """Fetch a file from treasury-cache via the GitHub Contents API.
    
    This is FRESH (no CDN cache) unlike raw.githubusercontent.com which has
    a ~5-min CDN cache. Authenticated with the autopilot PAT for higher
    rate limits (5,000/hr vs 60/hr unauth).
    
    Returns the decoded JSON content, or None on 404.
    """
    pat = settings.github_pat
    if not pat:
        logger.warning("No GitHub PAT configured for contents API fetch")
        return None

    url = f"https://api.github.com/repos/TrueSightDAO/treasury-cache/contents/{path}?ref=main"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TrueSightDAO-autopilot/1.0",
    }

    try:
        resp = httpx.get(url, headers=headers, timeout=15.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        # Contents API returns base64-encoded content
        import base64

        decoded = base64.b64decode(body["content"]).decode("utf-8")
        return json.loads(decoded)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        logger.warning("Contents API fetch failed for %s: %s", path, e)
        return None
    except Exception as e:
        logger.warning("Contents API fetch error for %s: %s", path, e)
        return None


def _fetch_per_key_raw(sha256_hex: str) -> dict | None:
    """Fetch a per-key file from raw.githubusercontent.com (warm-cache path).
    
    This has a ~5-min CDN cache but is faster for repeated lookups.
    Used as a fallback when the contents API is unavailable.
    """
    url = f"{_DEFAULT_PUBLIC_KEYS_BASE}/{sha256_hex}.json"
    try:
        resp = httpx.get(url, timeout=15.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        logger.warning("Raw fetch failed for %s: %s", sha256_hex, e)
        return None
    except Exception as e:
        logger.warning("Raw fetch error for %s: %s", sha256_hex, e)
        return None


def _governor_names() -> set[str]:
    raw = os.getenv("GOVERNOR_NAMES", "Gary Teh")
    return {name.strip() for name in raw.split(",") if name.strip()}


def _extract_governor_keys(members_data: dict) -> list[dict]:
    allowed = _governor_names()
    governors: list[dict] = []
    for contributor in members_data.get("contributors", []):
        name = contributor.get("name", "").strip()
        if name not in allowed:
            continue
        for key_entry in contributor.get("public_keys", []):
            if key_entry.get("status", "").upper() != "ACTIVE":
                continue
            governors.append(
                {
                    "public_key": key_entry["public_key"],
                    "name": name,
                    "email": contributor.get("email", ""),
                    "status": "Governor",
                    "key_created_at": key_entry.get("created_at", ""),
                    "key_last_active_at": key_entry.get("last_active_at", ""),
                }
            )
    return governors


def resolve_key(public_key_b64: str) -> dict | None:
    """Resolve a public key to its identity via content-addressed point-lookup.
    
    Returns a dict with {name, roles, is_governor, status} or None if the key
    is not found or not ACTIVE.
    
    Uses a short per-key in-memory cache (60s TTL). Falls back to the monolith
    if the per-key file is absent (backward compat during migration).
    """
    h = _sha256(public_key_b64)
    now = _now()

    # 1. Check per-key cache
    cached = _per_key_cache.get(h)
    if cached is not None and (now - cached["fetched_at"]) < _PER_KEY_CACHE_TTL:
        return cached["data"]

    # 2. Fetch from GitHub Contents API (fresh)
    key_data = _fetch_github_contents_api(f"public_keys/{h}.json")

    # 3. Fall back to raw.githubusercontent.com (warm-cache)
    if key_data is None:
        key_data = _fetch_per_key_raw(h)

    # 4. Fall back to monolith (backward compat during migration)
    if key_data is None:
        logger.debug("Per-key file not found for %s, falling back to monolith", h[:12])
        data = load_governors()
        for g in data.get("governors", []):
            if g.get("public_key") == public_key_b64:
                result = {
                    "name": g.get("name"),
                    "roles": ["governor", "member"],
                    "is_governor": True,
                    "status": "ACTIVE",
                    "email": g.get("email", ""),
                }
                _per_key_cache[h] = {"data": result, "fetched_at": now}
                return result
        _per_key_cache[h] = {"data": None, "fetched_at": now}
        return None

    # 5. Validate status
    status = (key_data.get("status") or "").upper()
    if status != "ACTIVE":
        _per_key_cache[h] = {"data": None, "fetched_at": now}
        return None

    # 6. Build result
    roles = key_data.get("roles", [])
    result = {
        "name": key_data.get("contributor"),
        "roles": roles,
        "is_governor": "governor" in roles,
        "status": status,
        "public_key": key_data.get("public_key"),
        "sha256": key_data.get("sha256"),
        "created_at": key_data.get("created_at"),
        "last_active_at": key_data.get("last_active_at"),
    }

    _per_key_cache[h] = {"data": result, "fetched_at": now}
    return result


def resolve_key_name(public_key_b64: str) -> str | None:
    """Resolve a public key to its governor name. Returns name or None."""
    identity = resolve_key(public_key_b64)
    if identity:
        return identity.get("name")
    return None


def load_governors(force_refresh: bool = False) -> dict:
    global _cache

    now = _now()
    cache_url = os.getenv("GOVERNORS_RAW_URL", _DEFAULT_MEMBERS_URL)

    if not force_refresh and _cache["data"] is not None:
        if (
            _cache["url"] == cache_url
            and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS
        ):
            return _cache["data"]

    try:
        members_data = _fetch_remote(cache_url)
        governors = _extract_governor_keys(members_data)
        data = {
            "version": 2,
            "updated_at": members_data.get("generated_at", ""),
            "source": cache_url,
            "governors": governors,
        }
        _cache["data"] = data
        _cache["fetched_at"] = now
        _cache["url"] = cache_url
        return data
    except Exception as exc:
        logger.warning("Failed to fetch remote dao_members.json: %s", exc)

    local_path = settings.static_governors_json
    if local_path is not None:
        if not local_path.is_absolute():
            repo_root = Path(__file__).resolve().parent.parent
            local_path = repo_root / local_path
        data = _load_local(local_path)
        if data is not None:
            _cache["data"] = data
            _cache["fetched_at"] = now
            _cache["url"] = str(local_path)
            return data

    return {
        "version": 2,
        "updated_at": "",
        "source": "deny-all",
        "governors": [],
    }


def is_governor(public_key_b64: str) -> bool:
    """Check if a public key belongs to a registered governor.
    
    Fast path: tries resolve_key() first (per-key point-lookup).
    Fallback: iterates the monolith (backward compat during migration).
    """
    # Fast path: point-lookup
    identity = resolve_key(public_key_b64)
    if identity is not None:
        return identity.get("is_governor", False)

    # Fallback: monolith iteration
    data = load_governors()
    governors = data.get("governors", [])
    for g in governors:
        if g.get("public_key") == public_key_b64:
            return True
    return False


def refresh_cache() -> dict:
    """Force-refresh the monolith cache and clear the per-key cache."""
    global _per_key_cache
    _per_key_cache = {}
    return load_governors(force_refresh=True)


def clear_per_key_cache() -> None:
    """Clear the per-key cache (for testing)."""
    global _per_key_cache
    _per_key_cache = {}
