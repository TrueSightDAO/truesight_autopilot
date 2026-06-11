"""Promote a reviewed beta deploy to its production fork — without cloning.

The three production sites deploy from forks of their beta repos
(``settings.prod_repos``). Beta-first flow: the change lands in beta, the
governor reviews the live beta deploy, and ONLY on the governor's explicit
approval does this tool promote it — via GitHub's ``merge-upstream`` endpoint
(the same mechanism as ``gh repo sync``): a fork sync on GitHub's side, no
clone, no local state, never force.

If the sync fails (merge conflict / non-fast-forward — e.g. the intentional
CNAME divergence between beta and prod has been disturbed), the tool reports
the error verbatim. NEVER attempt a force sync; a force overwrite of prod's
CNAME breaks the production domain binding. Escalate to the governor instead.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..config import settings
from ..tool_registry import ToolSpec


def sync_beta_to_prod(prod_repo: str) -> dict[str, Any]:
    if prod_repo not in settings.prod_repos:
        return {
            "status": "error",
            "message": (
                f"'{prod_repo}' is not a known production repo. Known prod repos: {sorted(settings.prod_repos)}"
            ),
        }
    if not settings.github_pat:
        return {
            "status": "error",
            "message": "TRUESIGHT_DAO_AUTOPILOT PAT not configured.",
        }

    url = f"https://api.github.com/repos/TrueSightDAO/{prod_repo}/merge-upstream"
    headers = {
        "Authorization": f"token {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = httpx.post(url, headers=headers, json={"branch": "main"}, timeout=30.0)
    except httpx.RequestError as exc:
        return {"status": "error", "message": f"Request failed: {exc}"}

    if resp.status_code == 200:
        data = resp.json()
        return {
            "status": "ok",
            "prod_repo": prod_repo,
            "beta_source": settings.prod_repos[prod_repo],
            "merge_type": data.get("merge_type"),
            "message": data.get("message", "Synced."),
        }
    if resp.status_code == 409:
        return {
            "status": "conflict",
            "message": (
                "Merge conflict syncing beta → prod (histories diverged, possibly "
                "the intentional CNAME divergence). DO NOT force. Report this to "
                "the governor — a human must reconcile."
            ),
        }
    return {
        "status": "error",
        "message": f"GitHub API {resp.status_code}: {resp.text[:300]}",
    }


def _handler(args: dict, ctx: dict) -> str:
    return json.dumps(sync_beta_to_prod(args.get("prod_repo", "")), indent=2)


TOOL_SPEC = ToolSpec(
    name="sync_beta_to_prod",
    description=(
        "Promote a reviewed beta deploy to production by syncing the prod fork "
        "from its beta base (GitHub merge-upstream — no clone, never force). "
        "ONLY call this after the governor has reviewed the beta deploy and "
        "EXPLICITLY approved promotion in this conversation. Prod repos: "
        "agroverse_shop_prod, truesight_me_prod, dapp_prod. On conflict, stop "
        "and report — never force-sync (CNAME divergence is intentional)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prod_repo": {
                "type": "string",
                "description": "Production repo to sync from its beta base.",
                "enum": ["agroverse_shop_prod", "truesight_me_prod", "dapp_prod"],
            },
        },
        "required": ["prod_repo"],
    },
    handler=_handler,
)
