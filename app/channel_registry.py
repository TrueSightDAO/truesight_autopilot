"""Server-side channel/category registry — Redis-backed, shared across clients.

Moves the client's localStorage channel overlay to a single shared store on the
brain so channels/categories are identical across origins and devices. Built-in
channels remain in each deployment's config.json (client-side); this registry
holds only user-created channels/categories and retired channel ids.

Reuses ``app.redis_claim.get_client()`` (same Redis the session-claim layer
uses). Fails soft: if Redis is unreachable the registry reads as empty and
writes are logged-and-dropped, so a chat turn is never stalled by the registry.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from . import redis_claim

logger = logging.getLogger(__name__)

REGISTRY_KEY = "truesight:channels:registry"

_lock = threading.Lock()


def _empty() -> dict:
    return {"channels": [], "categories": [], "retired": []}


def _load(client=None) -> dict:
    cli = client or redis_claim.get_client()
    try:
        raw = cli.get(REGISTRY_KEY)
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                data.setdefault("channels", [])
                data.setdefault("categories", [])
                data.setdefault("retired", [])
                return data
    except Exception as exc:
        logger.warning("channel_registry load failed: %s", exc)
    return _empty()


def _save(data: dict, client=None) -> None:
    cli = client or redis_claim.get_client()
    try:
        cli.set(REGISTRY_KEY, json.dumps(data))
    except Exception as exc:
        logger.warning("channel_registry save failed: %s", exc)


def get_registry() -> dict:
    with _lock:
        return _load()


def create_channel(category_name, name, visibility) -> dict:
    with _lock:
        data = _load()
        ch = {
            "id": "ch_" + uuid.uuid4().hex[:10],
            "categoryName": (category_name or "General").strip() or "General",
            "name": (name or "").strip() or "untitled",
            "visibility": "public" if visibility == "public" else "private",
            "created_at": int(time.time()),
        }
        data["channels"].append(ch)
        _save(data)
        return ch


def update_channel(channel_id, name=None, visibility=None):
    with _lock:
        data = _load()
        ch = next((c for c in data["channels"] if c["id"] == channel_id), None)
        if ch is None:
            return None
        if name is not None and str(name).strip():
            ch["name"] = str(name).strip()
        if visibility in ("public", "private"):
            ch["visibility"] = visibility
        _save(data)
        return ch


def delete_channel(channel_id) -> bool:
    with _lock:
        data = _load()
        if channel_id not in data["retired"]:
            data["retired"].append(channel_id)
        data["channels"] = [c for c in data["channels"] if c["id"] != channel_id]
        _save(data)
        return True


def create_category(name) -> str:
    with _lock:
        data = _load()
        n = (name or "").strip()
        if n and n not in data["categories"]:
            data["categories"].append(n)
            _save(data)
        return n


def delete_category(name) -> bool:
    with _lock:
        data = _load()
        data["categories"] = [c for c in data["categories"] if c != name]
        data["channels"] = [c for c in data["channels"] if c["categoryName"] != name]
        _save(data)
        return True


def rename_category(old_name, new_name) -> str:
    with _lock:
        data = _load()
        n = (new_name or "").strip()
        if not n or n == old_name:
            return old_name
        if old_name in data["categories"]:
            data["categories"] = [n if c == old_name else c for c in data["categories"]]
        for ch in data["channels"]:
            if ch.get("categoryName") == old_name:
                ch["categoryName"] = n
        _save(data)
        return n
