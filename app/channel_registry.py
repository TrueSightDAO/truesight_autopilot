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


def _clean_names(values) -> list[str]:
    """Keep only non-empty strings, trimmed. Drops None/garbage from client payloads."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name:
            out.append(name)
    return out


def _empty_layout() -> dict:
    """Placement overlay: explicit category order, per-channel placement, collapse set.

    Built-in channels/categories live in each deployment's config.json and cannot
    be mutated at runtime, so the sidebar's drag-and-drop is stored as an overlay
    on top of that immutable config -- exactly like ``retired`` above.
    """
    return {"category_order": [], "channels": {}, "collapsed": []}


def _empty() -> dict:
    return {
        "channels": [],
        "categories": [],
        "retired": [],
        "layout": _empty_layout(),
    }


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
                layout = data.setdefault("layout", _empty_layout())
                if not isinstance(layout, dict):
                    data["layout"] = layout = _empty_layout()
                layout.setdefault("category_order", [])
                layout.setdefault("channels", {})
                layout.setdefault("collapsed", [])
                return data
    except Exception as exc:  # noqa: BLE001 - registry must never break a page load
        logger.warning("channel_registry load failed: %s", exc)
    return _empty()


def _save(data: dict, client=None) -> None:
    cli = client or redis_claim.get_client()
    try:
        cli.set(REGISTRY_KEY, json.dumps(data))
    except Exception as exc:  # noqa: BLE001 - registry must never break a request
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


# ── layout overlay (sidebar drag-and-drop + collapse state) ──────────────────


def get_layout() -> dict:
    with _lock:
        return _load()["layout"]


def update_layout(payload: dict) -> dict:
    """Apply a partial layout update and return the full layout.

    Accepts any subset of:
      ``category_order`` -- full ordered list of category names
      ``move``           -- {"channel_id": str, "category_name": str,
                            "position": int|None}
      ``collapsed``      -- full list of collapsed category names

    Unknown payload keys are ignored so the client can send the whole view state
    without the server having to know every field. Returns the merged layout.
    """
    payload = payload or {}
    with _lock:
        data = _load()
        layout = data["layout"]

        order = payload.get("category_order")
        if isinstance(order, list):
            layout["category_order"] = _clean_names(order)

        collapsed = payload.get("collapsed")
        if isinstance(collapsed, list):
            layout["collapsed"] = _clean_names(collapsed)

        move = payload.get("move")
        if isinstance(move, dict):
            channel_id = str(move.get("channel_id") or "").strip()
            if channel_id:
                placement = layout["channels"].get(channel_id) or {}
                category_name = move.get("category_name")
                if isinstance(category_name, str) and category_name.strip():
                    placement["category_name"] = category_name.strip()
                position = move.get("position")
                if isinstance(position, int) and position >= 0:
                    placement["position"] = position
                if placement:
                    layout["channels"][channel_id] = placement

        _save(data)
        return layout
