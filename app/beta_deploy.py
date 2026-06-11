"""Beta-deploy gate (Telegram /ship) — roadmap B5/B6.

Lets the governor ship a PR to a **beta** repo from Telegram: check CI is green,
merge it (squash), and the beta site auto-deploys (GitHub Pages). Prod repos are
never touched here — promotion to prod stays manual (`gh repo sync`).

Safety:
- Master switch `BETA_DEPLOY_GATE_ENABLED` (default OFF) — inert until enabled.
- Only repos in `beta_deploy_repos` can be merged.
- Never merges unless CI is verified green.
- B5 = one-tap confirm; B6 (`BETA_AUTO_MERGE`) skips the tap when CI is green.
"""

from __future__ import annotations

import logging
import re

from .config import settings
from .github_client import GitHubClient

logger = logging.getLogger("autopilot.beta_deploy")


# ── Pure helpers (unit-tested) ─────────────────────────────────────────────


def beta_repos() -> list[str]:
    return list(settings.beta_deploy_repos)


def is_beta_repo(repo: str) -> bool:
    return repo in set(settings.beta_deploy_repos)


def parse_ship_target(text: str) -> tuple[str, int] | None:
    """Parse `/ship`, `/ship dapp_beta#12`, `/ship dapp_beta 12`, `/ship #12`.
    Returns (repo, pr) or None when no PR number is given (→ list mode)."""
    body = text[len("/ship") :].strip() if text.startswith("/ship") else text.strip()
    if not body:
        return None
    m = re.match(r"^([A-Za-z0-9_.-]+)?\s*#?\s*(\d+)$", body)
    if not m:
        return None
    repo = m.group(1) or (beta_repos()[0] if beta_repos() else "")
    return (repo, int(m.group(2))) if repo else None


def parse_callback_data(data: str) -> tuple[str, str | None, int | None]:
    """`ship:dapp_beta:12` → ('ship','dapp_beta',12); `cancel` → ('cancel',None,None)."""
    parts = (data or "").split(":")
    action = parts[0] if parts else ""
    if action == "ship" and len(parts) == 3 and parts[2].isdigit():
        return ("ship", parts[1], int(parts[2]))
    return (action or "cancel", None, None)


def build_ship_keyboard(repo: str, pr: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"🚀 Ship #{pr} → {repo}",
                    "callback_data": f"ship:{repo}:{pr}",
                },
                {"text": "✕ Cancel", "callback_data": "cancel"},
            ]
        ]
    }


# ── GitHub-backed (mocked in tests) ────────────────────────────────────────


def check_ci_green(repo: str, pr_number: int) -> tuple[bool, str]:
    """True only if every check on the PR head has completed successfully."""
    try:
        gh = GitHubClient()
        r = gh.g.get_repo(gh._full_name(repo))
        pull = r.get_pull(pr_number)
        commit = r.get_commit(pull.head.sha)
        runs = list(commit.get_check_runs())
        pending = [cr.name for cr in runs if cr.status != "completed"]
        failed = [
            cr.name
            for cr in runs
            if cr.status == "completed"
            and cr.conclusion not in ("success", "neutral", "skipped")
        ]
        state = commit.get_combined_status().state  # success / pending / failure
        if pending:
            return False, f"CI still running: {', '.join(pending)}"
        if failed:
            return False, f"CI failed: {', '.join(failed)}"
        if state in ("failure", "error"):
            return False, f"status checks: {state}"
        if not runs and state == "pending":
            return False, "no checks reported yet"
        return True, "green"
    except Exception as e:  # noqa: BLE001 — never merge if we can't verify
        logger.warning("check_ci_green failed for %s#%s: %s", repo, pr_number, e)
        return False, f"could not verify CI ({e})"


def ship_pr(repo: str, pr_number: int) -> dict:
    """Gate + CI check + merge into a beta repo. Returns {ok, message, sha?}."""
    if not settings.beta_deploy_gate_enabled:
        return {
            "ok": False,
            "message": "Beta-deploy gate is disabled (set BETA_DEPLOY_GATE_ENABLED=true).",
        }
    if not is_beta_repo(repo):
        return {
            "ok": False,
            "message": f"'{repo}' is not a beta repo (allowed: {', '.join(beta_repos())}). Prod is manual-promote.",
        }
    green, summary = check_ci_green(repo, pr_number)
    if not green:
        return {"ok": False, "message": f"Not shipping {repo}#{pr_number} — {summary}."}
    res = GitHubClient().merge_pr(repo, pr_number, merge_method="squash")
    if res.get("merged"):
        return {
            "ok": True,
            "sha": res.get("sha", ""),
            "message": f"✅ Merged {repo}#{pr_number} (CI green) — beta is deploying.",
        }
    return {
        "ok": False,
        "message": f"Merge failed: {res.get('message', 'unknown error')}",
    }


def list_open_beta_prs() -> list[dict]:
    """Open PRs across the configured beta repos: [{repo, number, title, url}]."""
    out: list[dict] = []
    gh = GitHubClient()
    for repo in beta_repos():
        try:
            for pr in gh.list_prs(repo, state="open", limit=10):
                out.append(
                    {
                        "repo": repo,
                        "number": pr.get("number"),
                        "title": pr.get("title", ""),
                        "url": pr.get("url", ""),
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("list_open_beta_prs failed for %s: %s", repo, e)
    return out
