"""Promote a reviewed beta deploy to its production fork — without cloning.

The three production sites deploy from forks of their beta repos
(``settings.prod_repos``). Beta-first flow: the change lands in beta, the
governor reviews the live beta deploy, and ONLY on the governor's explicit
approval does this tool promote it — via GitHub's ``merge-upstream`` endpoint
(the same mechanism as ``gh repo sync``): a fork sync on GitHub's side, no
clone, no local state, never force.

On a merge conflict (409) the tool auto-reconciles ONLY the governor-blessed
AUTO-GENERATED paths in ``settings.prod_sync_generated_globs`` (both forks run
the same generators, so their scheduled "chore(stats): refresh" commits diverge
those files and block the sync even when the reviewed change itself is clean) —
it takes BETA's copy of each and retries ONCE. Anything outside those globs is
never touched: if a conflict remains, the tool reports it verbatim. NEVER
attempt a force sync; a force overwrite of prod's CNAME breaks the production
domain binding. Escalate to the governor instead.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("autopilot.tools.sync_beta_to_prod")

# Single source of truth: derive the schema enum (and the human-facing
# description) from settings.prod_repos so a prod repo added to config is
# callable through this tool with zero tool edits. Regression-guarded by
# tests/test_sync_beta_to_prod_tool.py.
_PROD_REPOS = sorted(settings.prod_repos)
# Upper bound on how many blessed generated files a single 409 auto-resolve may
# rewrite. A divergence this large is not a routine stats refresh — stop and
# report rather than mass-overwrite.
_MAX_AUTO_RESOLVE = 50
from ..deploy_ledger import (
    acquire_lease,
    append_deploy_record,
    check_lease,
    close_lease,
)
from ..tool_registry import ToolSpec


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"token {settings.github_pat}",
        "Accept": "application/vnd.github+json",
    }


def _list_dir(repo: str, prefix: str) -> dict[str, str]:
    """Return ``{path: blob_sha}`` for files directly under ``prefix``.

    ``prefix == ""`` lists the repo root. Best-effort: any transport/HTTP error
    yields ``{}`` so the sync degrades to the plain conflict report, never raises.
    """
    sub = f"/contents/{prefix}" if prefix else "/contents"
    url = f"https://api.github.com/repos/TrueSightDAO/{repo}{sub}?ref=main"
    try:
        resp = httpx.get(url, headers=_gh_headers(), timeout=30.0)
    except httpx.RequestError:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        entries = resp.json()
    except ValueError:
        return {}
    if not isinstance(entries, list):
        return {}
    return {
        e["path"]: e.get("sha", "")
        for e in entries
        if e.get("type") == "file" and "path" in e
    }


def _copy_beta_file_to_prod(
    headers: dict[str, str],
    prod_repo: str,
    beta_repo: str,
    path: str,
    prod_sha: str | None,
) -> bool:
    """Overwrite prod's copy of ``path`` with beta's (Contents API, one commit)."""
    src = (
        f"https://api.github.com/repos/TrueSightDAO/{beta_repo}"
        f"/contents/{path}?ref=main"
    )
    try:
        r = httpx.get(src, headers=headers, timeout=30.0)
    except httpx.RequestError:
        return False
    if r.status_code != 200:
        return False
    try:
        meta = r.json()
    except ValueError:
        return False
    content_b64 = (meta.get("content") or "").replace("\n", "")
    if not content_b64:
        return False
    body: dict[str, Any] = {
        "message": (
            f"chore(sync): take beta copy of generated {path} [prod-sync auto-resolve]"
        ),
        "content": content_b64,
        "branch": "main",
    }
    if prod_sha:
        body["sha"] = prod_sha
    dst = f"https://api.github.com/repos/TrueSightDAO/{prod_repo}/contents/{path}"
    try:
        put = httpx.put(dst, headers=headers, json=body, timeout=30.0)
    except httpx.RequestError:
        return False
    return put.status_code in (200, 201)


def _reconcile_generated_files(prod_repo: str, beta_repo: str) -> dict[str, list[str]]:
    """Take BETA's copy of every blessed generated path that diverged.

    Returns ``{"resolved": [...], "failed": [...]}`` (repo-relative paths). Only
    paths matching ``settings.prod_sync_generated_globs`` are ever touched; a
    hard cap guards against a runaway mass-overwrite.
    """
    resolved: list[str] = []
    failed: list[str] = []
    globs = list(settings.prod_sync_generated_globs)
    if not globs:
        return {"resolved": resolved, "failed": failed}

    prefixes = sorted({g.rsplit("/", 1)[0] if "/" in g else "" for g in globs})
    headers = _gh_headers()
    candidates: list[tuple[str, str, str | None]] = []
    for prefix in prefixes:
        prod_files = _list_dir(prod_repo, prefix)
        beta_files = _list_dir(beta_repo, prefix)
        for path, beta_sha in sorted(beta_files.items()):
            if not any(fnmatch.fnmatchcase(path, g) for g in globs):
                continue
            prod_sha = prod_files.get(path)
            if prod_sha != beta_sha:
                candidates.append((path, beta_sha, prod_sha))

    if len(candidates) > _MAX_AUTO_RESOLVE:
        # Too big to be a routine generated-file refresh — do not mass-write.
        return {"resolved": [], "failed": [p for p, _, _ in candidates]}

    for path, _beta_sha, prod_sha in candidates:
        if _copy_beta_file_to_prod(headers, prod_repo, beta_repo, path, prod_sha):
            resolved.append(path)
        else:
            failed.append(path)
    return {"resolved": resolved, "failed": failed}


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

    # DEPLOY_PUSH_SOP Phase 2: soft-lock lease on the prod repo before sync.
    lease_id = ""
    lease = check_lease("prod-sync", prod_repo)
    if lease.get("status") == "blocked":
        return {
            "status": "error",
            "message": (
                "push blocked by a live deploy lease (DEPLOY_PUSH_SOP). "
                f"Leases: {lease.get('leases', [])}. Wait for them to close (TTL 30 min)."
            ),
        }
    if lease.get("status") == "error":
        logger.warning(
            "deploy_ledger: lease check failed open for %s: %s",
            prod_repo,
            lease.get("reason", lease.get("error", "?")),
        )
    acq = acquire_lease("prod-sync", prod_repo, f"sync_beta_to_prod {prod_repo}")
    if acq.get("status") == "success":
        lease_id = acq.get("lease_id", "")
        logger.info(
            "deploy_ledger: acquired lease %s for prod-sync %s", lease_id, prod_repo
        )

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
        rec = append_deploy_record(
            agent="sophia",
            target_type="prod-sync",
            target_id=prod_repo,
            action=f"sync_beta_to_prod {prod_repo}",
            result="success",
            evidence_url=f"https://github.com/TrueSightDAO/{prod_repo}/commits/main",
            lease_id=lease_id,
            notes="sync_beta_to_prod autopilot tool (DEPLOY_PUSH_SOP Phase 2)",
        )
        if rec.get("status") != "success":
            logger.warning("deploy_ledger: record append failed: %s", rec.get("error"))
        if lease_id:
            close_lease(lease_id)
        return {
            "status": "ok",
            "prod_repo": prod_repo,
            "beta_source": settings.prod_repos[prod_repo],
            "merge_type": data.get("merge_type"),
            "message": data.get("message", "Synced."),
            "deploy_ledger": rec,
        }
    if resp.status_code == 409:
        # Auto-resolve ONLY governor-blessed AUTO-GENERATED files. Both forks run
        # the same generators on a schedule, so their "chore(stats): refresh" bot
        # commits diverge those files and block merge-upstream with a 409 — even
        # when the reviewed change itself merges cleanly. Reconcile by taking
        # BETA's copy of the blessed paths, then retry the sync ONCE. Anything
        # outside settings.prod_sync_generated_globs is never touched: if that
        # leaves a conflict we stop and report for human reconcile.
        beta_repo = settings.prod_repos[prod_repo]
        reconcile = _reconcile_generated_files(prod_repo, beta_repo)

        if reconcile["resolved"]:
            try:
                retry = httpx.post(
                    url, headers=headers, json={"branch": "main"}, timeout=30.0
                )
            except httpx.RequestError:
                retry = None
            if retry is not None and retry.status_code == 200:
                data = retry.json()
                rec = append_deploy_record(
                    agent="sophia",
                    target_type="prod-sync",
                    target_id=prod_repo,
                    action=f"sync_beta_to_prod {prod_repo}",
                    result="success",
                    evidence_url=(
                        f"https://github.com/TrueSightDAO/{prod_repo}/commits/main"
                    ),
                    lease_id=lease_id,
                    notes=(
                        "sync_beta_to_prod: auto-resolved generated files "
                        f"{reconcile['resolved']} then merge-upstream succeeded"
                    ),
                )
                if rec.get("status") != "success":
                    logger.warning(
                        "deploy_ledger: record append failed: %s", rec.get("error")
                    )
                if lease_id:
                    close_lease(lease_id)
                return {
                    "status": "ok",
                    "prod_repo": prod_repo,
                    "beta_source": beta_repo,
                    "merge_type": data.get("merge_type"),
                    "message": data.get("message", "Synced."),
                    "auto_resolved": reconcile["resolved"],
                    "deploy_ledger": rec,
                }

        append_deploy_record(
            agent="sophia",
            target_type="prod-sync",
            target_id=prod_repo,
            action=f"sync_beta_to_prod {prod_repo}",
            result="failure",
            evidence_url="",
            lease_id=lease_id,
            notes=(
                "409 merge conflict — histories diverged; DO NOT force. "
                f"auto_resolved={reconcile['resolved']} "
                f"auto_resolve_failed={reconcile['failed']}. Human reconcile."
            ),
        )
        if lease_id:
            close_lease(lease_id)
        return {
            "status": "conflict",
            "message": (
                "Merge conflict syncing beta → prod (histories diverged). DO NOT "
                "force. "
                + (
                    f"Auto-resolved blessed generated files {reconcile['resolved']} "
                    "but a conflict remains outside the blessed generated set — a "
                    "human must reconcile."
                    if reconcile["resolved"]
                    else "No blessed generated files diverged; the conflict is "
                    "outside settings.prod_sync_generated_globs — a human must "
                    "reconcile."
                )
            ),
            "auto_resolved": reconcile["resolved"],
            "auto_resolve_failed": reconcile["failed"],
        }
    append_deploy_record(
        agent="sophia",
        target_type="prod-sync",
        target_id=prod_repo,
        action=f"sync_beta_to_prod {prod_repo}",
        result="failure",
        evidence_url="",
        lease_id=lease_id,
        notes=f"GitHub API {resp.status_code}: {resp.text[:200]}",
    )
    if lease_id:
        close_lease(lease_id)
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
        f"{', '.join(_PROD_REPOS)}. On conflict, blessed auto-generated paths "
        "(settings.prod_sync_generated_globs) are auto-reconciled from beta and "
        "the sync is retried once; any remaining conflict stops and is reported "
        "— never force-sync (CNAME divergence is intentional)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prod_repo": {
                "type": "string",
                "description": "Production repo to sync from its beta base.",
                "enum": _PROD_REPOS,
            },
        },
        "required": ["prod_repo"],
    },
    handler=_handler,
)
