"""Governor registry loader — reads dao_members.json from treasury-cache (GitHub raw).

resolve_key() uses raw.githubusercontent.com for content-addressed point-lookup
via public_keys/<sha256>.json.
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
_RAW_KEY_BASE = "https://raw.githubusercontent.com/TrueSightDAO/treasury-cache/main/public_keys"
_CACHE_TTL_SECONDS = int(os.getenv("GOVERNORS_CACHE_TTL", "300"))

# Per-key cache: sha256 -> (fetched_at, data_or_None)
# Short TTL (60s) so a freshly-registered key is recognized quickly.
_PER_KEY_CACHE_TTL = int(os.getenv("PER_KEY_CACHE_TTL", "60"))
_per_key_cache: dict[str, tuple[float, dict | None]] = {}

_cache: dict[str, any] = {
    "data": None,
    "fetched_at": 0.0,
    "url": None,
}


def _now() -> float:
    return time.time()


def _sha256(s: str) -> str:
    """Compute SHA-256 hex digest of a string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_local(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _fetch_remote(url: str) -> dict | None:
    """Fetch a JSON file from a URL. Returns None on 404 or error."""
    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("fetch failed for %s: %s", url, exc)
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

    Returns a dict with {name, is_governor, email, roles} or None if the key
    is not found or not ACTIVE.

    Uses raw.githubusercontent.com (CDN-cached, ~5 min TTL). The force-fresh
    retry in is_governor() handles the edge case of a freshly-registered key.
    """
    h = _sha256(public_key_b64)
    now = _now()

    # 1. Check per-key cache
    cached = _per_key_cache.get(h)
    if cached is not None and (now - cached[0]) < _PER_KEY_CACHE_TTL:
        return cached[1]

    # 2. Fetch from raw GitHub CDN
    url = f"{_RAW_KEY_BASE}/{h}.json"
    key_data = _fetch_remote(url)

    if key_data is None:
        # 404 or error — cache as None (short TTL so retries happen quickly)
        _per_key_cache[h] = (now, None)
        return None

    # 3. Validate status
    status = (key_data.get("status") or "").upper()
    if status != "ACTIVE":
        _per_key_cache[h] = (now, None)
        return None

    # 4. Build identity
    roles = key_data.get("roles", [])
    identity = {
        "name": key_data.get("contributor", "Unknown"),
        "is_governor": "governor" in roles,
        "email": "",  # email omitted from per-key files (privacy decision)
        "roles": roles,
    }

    _per_key_cache[h] = (now, identity)
    return identity


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
        if members_data is None:
            raise ValueError(f"failed to fetch {cache_url}")
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

    Uses resolve_key() for fast point-lookup first. Falls back to
    load_governors() enumeration if resolve_key returns None.

    Freshness: if resolve_key returns None (miss or non-ACTIVE), does ONE
    force-fresh lookup (clears per-key cache and retries) before refusing.
    This handles the ~5-min CDN cache on raw.githubusercontent.com.
    """
    # 1. Try fast point-lookup
    identity = resolve_key(public_key_b64)
    if identity is not None and identity.get("is_governor"):
        return True

    # 2. Force-fresh lookup: clear the per-key cache and retry
    h = _sha256(public_key_b64)
    _per_key_cache.pop(h, None)
    identity = resolve_key(public_key_b64)
    if identity is not None and identity.get("is_governor"):
        return True

    # 3. Fallback to monolith enumeration
    data = load_governors()
    governors = data.get("governors", [])
    for g in governors:
        if g.get("public_key") == public_key_b64:
            return True
    return False


def refresh_cache() -> dict:
    """Force-refresh the monolith cache."""
    return load_governors(force_refresh=True)


def clear_per_key_cache() -> None:
    """Clear the per-key cache (e.g. after a deploy or manual refresh)."""
    _per_key_cache.clear()
