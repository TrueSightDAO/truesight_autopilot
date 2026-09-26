"""Create / rename / move / delete Discord **channels and categories**.

Companion to ``app/tools/discord_attachment.py``: the Discord adapter could
read messages and post replies, but had **no admin surface** — so a server
re-organisation (new channels, a category tree, archiving dead channels) could
only be done by hand in the Discord UI. This closes that gap.

A Discord **category is just a channel of type 4**, so one tool family covers
both "channels" and "folders":

    create_discord_channel(name, channel_type="category")  # a folder
    create_discord_channel(name, category_id=<cat>)        # a channel in it
    update_discord_channel(channel_id, name=..., category_id=..., position=...)
    delete_discord_channel(channel_id, confirm=True)       # irreversible
    list_discord_channels()                                # read the tree

**Safety (matches the repo's conventions):**
  * Every mutating call defaults to ``dry_run=True`` — it returns the exact
    payload it *would* send and touches nothing. Executing requires an explicit
    ``dry_run=False``.
  * ``delete_discord_channel`` additionally requires ``confirm=True`` — an
    irreversible delete needs a second, deliberate flag.
  * All three mutating tools are registered as **WRITE** in ``app/policy.py``,
    so the tool-layer policy gate (Security invariant #1) blocks any caller who
    is not a verified governor. ``delete_discord_channel`` is additionally
    ``default_roles={"governor"}``.

**Permission required (once, operator):** Sophia's bot role
(``Sophia TrueSight``) needs **Manage Channels** in the guild — or
**Administrator**. Verified 2026-09 (bot holds Administrator).

Target guild resolution order: explicit ``guild_id`` arg → the guild in a
``dc:{guild}:{channel}`` session id → ``settings.discord_guild_id``.
"""

from __future__ import annotations

import json
import logging

from ..config import settings
from ..tool_registry import ToolSpec

logger = logging.getLogger("autopilot.tools.discord_admin")

# Discord channel types we expose by friendly name (Discord API "type" ints).
_CHANNEL_TYPES: dict[str, int] = {
    "text": 0,
    "voice": 2,
    "category": 4,
    "announcement": 5,
    "stage": 13,
    "forum": 15,
}
_TYPE_NAMES: dict[int, str] = {v: k for k, v in _CHANNEL_TYPES.items()}


def _guild_id_from_session(session_id: str | None) -> str | None:
    """Recover the guild id from a ``dc:{guild}:{channel}`` session id.

    Mirrors ``_channel_id_from_session`` in ``discord_attachment.py``, one
    segment over. ``build_session_id`` in ``discord_adapter.py`` writes this
    shape; the brain may prefix it, so look for the ``dc`` token anywhere.
    """
    if not session_id:
        return None
    parts = session_id.split(":")
    if "dc" in parts:
        i = parts.index("dc")
        if i + 1 < len(parts) and parts[i + 1]:
            return parts[i + 1]
    return None


def _resolve_guild_id(guild_id: str | None, session_id: str | None) -> str | None:
    explicit = (guild_id or "").strip()
    if explicit:
        return explicit
    from_session = _guild_id_from_session(session_id)
    if from_session:
        return from_session
    configured = (settings.discord_guild_id or "").strip()
    return configured or None


def _type_name(t: object) -> str:
    try:
        return _TYPE_NAMES.get(int(t), f"type{int(t)}")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"


def list_discord_channels(
    guild_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Read the guild's channel tree (categories + the channels under them).

    Read-only; no governor gate needed but harmless if a guest calls it.
    """
    gid = _resolve_guild_id(guild_id, session_id)
    if not gid:
        return {
            "status": "error",
            "reason": "no guild id — pass guild_id, be in a dc: session, or set DISCORD_GUILD_ID",
        }
    from ..discord_adapter import _api

    data = _api("GET", f"/guilds/{gid}/channels")
    if data is None:
        return {
            "status": "error",
            "reason": "Discord list-channels call failed",
            "hint": "Ensure Sophia's bot can view this guild (View Channels).",
            "guild_id": gid,
        }
    if not isinstance(data, list):
        return {
            "status": "error",
            "reason": "unexpected response shape",
            "guild_id": gid,
        }

    cats = [c for c in data if int(c.get("type", -1)) == 4]
    lines: list[str] = []
    for cat in sorted(cats, key=lambda c: c.get("position", 0)):
        lines.append(f"[category] {cat.get('name')} ({cat.get('id')})")
        kids = [c for c in data if c.get("parent_id") == cat.get("id")]
        for ch in sorted(kids, key=lambda c: c.get("position", 0)):
            lines.append(
                f"  - {ch.get('name')} [{_type_name(ch.get('type'))}] ({ch.get('id')})"
            )
    uncategorised = [
        c for c in data if int(c.get("type", -1)) != 4 and not c.get("parent_id")
    ]
    for ch in sorted(uncategorised, key=lambda c: c.get("position", 0)):
        lines.append(
            f"(no category) {ch.get('name')} [{_type_name(ch.get('type'))}] ({ch.get('id')})"
        )

    return {
        "status": "ok",
        "guild_id": gid,
        "counts": {
            "total": len(data),
            "categories": len(cats),
            "channels": len(data) - len(cats),
        },
        "tree": "\n".join(lines),
    }


def create_discord_channel(
    name: str,
    channel_type: str = "text",
    category_id: str | None = None,
    guild_id: str | None = None,
    session_id: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Create a channel (or a category when ``channel_type="category"``).

    ``dry_run=True`` (default) composes the payload and returns it WITHOUT
    calling the Discord API. Pass ``dry_run=False`` to actually create.
    """
    name = (name or "").strip()
    if not name:
        return {"status": "error", "reason": "channel name is required"}
    ctype = _CHANNEL_TYPES.get((channel_type or "").strip().lower())
    if ctype is None:
        return {
            "status": "error",
            "reason": f"unknown channel_type {channel_type!r}",
            "allowed": sorted(_CHANNEL_TYPES),
        }
    gid = _resolve_guild_id(guild_id, session_id)
    if not gid:
        return {
            "status": "error",
            "reason": "no guild id — pass guild_id, be in a dc: session, or set DISCORD_GUILD_ID",
        }

    payload: dict[str, object] = {"name": name, "type": ctype}
    if category_id:
        payload["parent_id"] = str(category_id)

    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "action": "create",
            "guild_id": gid,
            "payload": payload,
            "message": f"WOULD create {channel_type} {name!r} in guild {gid}",
        }

    from ..discord_adapter import _api

    res = _api("POST", f"/guilds/{gid}/channels", payload)
    if res is None:
        return {
            "status": "error",
            "reason": "Discord create-channel call failed",
            "hint": "Ensure Sophia's bot role has Manage Channels (or Administrator) in this guild.",
            "guild_id": gid,
            "payload": payload,
        }
    logger.info("created Discord channel %r (%s) in guild %s", name, res.get("id"), gid)
    return {
        "status": "ok",
        "action": "created",
        "guild_id": gid,
        "channel_id": str(res.get("id")),
        "name": res.get("name"),
        "type": _type_name(res.get("type")),
        "parent_id": res.get("parent_id"),
    }


def update_discord_channel(
    channel_id: str,
    name: str | None = None,
    category_id: str | None = None,
    position: int | None = None,
    guild_id: str | None = None,
    session_id: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Rename and/or move a channel — the reversible reorg primitive.

    ``category_id=""`` moves the channel to the top level (no parent).
    ``dry_run=True`` (default) returns the payload without calling the API.
    """
    cid = str(channel_id or "").strip()
    if not cid:
        return {"status": "error", "reason": "channel_id is required"}

    payload: dict[str, object] = {}
    if name is not None and name.strip():
        payload["name"] = name.strip()
    if category_id is not None:
        # explicit empty string => move to top level (parent_id null)
        payload["parent_id"] = str(category_id) if str(category_id).strip() else None
    if position is not None:
        payload["position"] = int(position)

    if not payload:
        return {
            "status": "error",
            "reason": "nothing to update (pass name, category_id, or position)",
        }

    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "action": "update",
            "channel_id": cid,
            "payload": payload,
            "message": f"WOULD update channel {cid}: {payload}",
        }

    from ..discord_adapter import _api

    res = _api("PATCH", f"/channels/{cid}", payload)
    if res is None:
        return {
            "status": "error",
            "reason": "Discord update-channel call failed",
            "hint": "Ensure Sophia's bot role has Manage Channels (or Administrator) in this guild.",
            "channel_id": cid,
            "payload": payload,
        }
    logger.info("updated Discord channel %s: %s", cid, payload)
    return {
        "status": "ok",
        "action": "updated",
        "channel_id": str(res.get("id")),
        "name": res.get("name"),
        "type": _type_name(res.get("type")),
        "parent_id": res.get("parent_id"),
        "position": res.get("position"),
    }


def delete_discord_channel(
    channel_id: str,
    confirm: bool = False,
    session_id: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Delete a channel or category — **IRREVERSIBLE** (its messages go too).

    Requires BOTH ``dry_run=False`` and ``confirm=True``. Confirm the channel
    is genuinely dead (or better, archive it by renaming/moving it) first.
    """
    cid = str(channel_id or "").strip()
    if not cid:
        return {"status": "error", "reason": "channel_id is required"}

    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "action": "delete",
            "channel_id": cid,
            "message": (
                f"WOULD permanently delete channel/category {cid}. "
                "This is irreversible. Re-run with dry_run=false AND confirm=true to proceed."
            ),
        }

    if not confirm:
        return {
            "status": "refused",
            "reason": "delete is irreversible — pass confirm=true (and dry_run=false) to proceed.",
            "channel_id": cid,
        }

    from ..discord_adapter import _api

    res = _api("DELETE", f"/channels/{cid}")
    if res is None:
        return {
            "status": "error",
            "reason": "Discord delete-channel call failed",
            "hint": "Ensure Sophia's bot role has Manage Channels (or Administrator) in this guild.",
            "channel_id": cid,
        }
    logger.info("deleted Discord channel/category %s", cid)
    return {"status": "ok", "action": "deleted", "channel_id": cid}


# ── Tool specs ───────────────────────────────────────────────────────────────

_LIST_SPEC = ToolSpec(
    name="list_discord_channels",
    description=(
        "Read the TrueSight DAO Discord guild's channel tree: categories and the "
        "channels nested under them, with each channel's id and type. Read-only. "
        "Use this FIRST before any create/move/delete so you act on real ids "
        "rather than guessing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "guild_id": {
                "type": "string",
                "description": "Optional guild id; defaults to the current dc: session's guild or DISCORD_GUILD_ID.",
            }
        },
        "required": [],
    },
    handler=lambda args, ctx: json.dumps(
        list_discord_channels(
            guild_id=args.get("guild_id"),
            session_id=ctx.get("session_id"),
        ),
        indent=2,
    ),
    default_roles=None,
)

_CREATE_SPEC = ToolSpec(
    name="create_discord_channel",
    description=(
        "Create a Discord channel or CATEGORY (a category is just a folder of "
        "channels — channel_type='category'). Pass category_id to nest a channel "
        "inside an existing category. DEFAULTS TO DRY-RUN: it returns the payload "
        "it would send and creates nothing; pass dry_run=false to actually create. "
        "Requires the bot's Manage Channels permission."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Channel/category name."},
            "channel_type": {
                "type": "string",
                "enum": ["text", "voice", "category", "announcement", "stage", "forum"],
                "description": "Defaults to 'text'. Use 'category' to create a folder.",
            },
            "category_id": {
                "type": "string",
                "description": "Optional parent category id to nest this channel under.",
            },
            "guild_id": {
                "type": "string",
                "description": "Optional guild id override.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Default true (compose only). Set false to actually create.",
            },
        },
        "required": ["name"],
    },
    handler=lambda args, ctx: json.dumps(
        create_discord_channel(
            name=args.get("name", ""),
            channel_type=args.get("channel_type", "text"),
            category_id=args.get("category_id"),
            guild_id=args.get("guild_id"),
            session_id=ctx.get("session_id"),
            dry_run=args.get("dry_run", True),
        ),
        indent=2,
    ),
    default_roles=None,
)

_UPDATE_SPEC = ToolSpec(
    name="update_discord_channel",
    description=(
        "Rename and/or MOVE a Discord channel — the reversible reorg primitive. "
        "Set category_id to move a channel into a category (category_id='' moves it "
        "to the top level), and/or position to reorder. DEFAULTS TO DRY-RUN: returns "
        "the payload, changes nothing; pass dry_run=false to apply. Prefer this over "
        "delete for archiving."
    ),
    parameters={
        "type": "object",
        "properties": {
            "channel_id": {
                "type": "string",
                "description": "Id of the channel/category to change.",
            },
            "name": {"type": "string", "description": "New name (optional)."},
            "category_id": {
                "type": "string",
                "description": "Parent category id to move under; empty string to move to top level.",
            },
            "position": {
                "type": "integer",
                "description": "Sort position within its parent (optional).",
            },
            "guild_id": {
                "type": "string",
                "description": "Optional guild id override.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Default true (compose only). Set false to actually apply.",
            },
        },
        "required": ["channel_id"],
    },
    handler=lambda args, ctx: json.dumps(
        update_discord_channel(
            channel_id=args.get("channel_id", ""),
            name=args.get("name"),
            category_id=args.get("category_id"),
            position=args.get("position"),
            guild_id=args.get("guild_id"),
            session_id=ctx.get("session_id"),
            dry_run=args.get("dry_run", True),
        ),
        indent=2,
    ),
    default_roles=None,
)

_DELETE_SPEC = ToolSpec(
    name="delete_discord_channel",
    description=(
        "Permanently DELETE a Discord channel or category (and its messages) — "
        "IRREVERSIBLE. Requires dry_run=false AND confirm=true, both explicit. "
        "Strongly prefer update_discord_channel (rename + move to an archive "
        "category) unless the channel is genuinely dead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "channel_id": {
                "type": "string",
                "description": "Id of the channel/category to delete.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to actually delete (second deliberate flag).",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Default true (compose only). Must be false, with confirm=true, to delete.",
            },
        },
        "required": ["channel_id"],
    },
    handler=lambda args, ctx: json.dumps(
        delete_discord_channel(
            channel_id=args.get("channel_id", ""),
            confirm=args.get("confirm", False),
            session_id=ctx.get("session_id"),
            dry_run=args.get("dry_run", True),
        ),
        indent=2,
    ),
    default_roles=frozenset({"governor"}),  # irreversible — governor only
)

TOOL_SPECS = [_LIST_SPEC, _CREATE_SPEC, _UPDATE_SPEC, _DELETE_SPEC]
