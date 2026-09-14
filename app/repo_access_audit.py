"""Repo-access audit log (SOPHIA_REPO_ACCESS_DENYLIST_PLAN PR4).

Under the default-allow model the master ``allowed_repos`` list no longer
records *which* repos agents actually write to. This restores that
observability: every successful agent write appends a record to
``TrueSightDAO/ecosystem_change_logs/repo_access/`` (API-only repo -- Contents
API only, never a clone), tagged with the gate that authorised it so
default-allowed writes stay distinguishable from strict-listed ones.

Fail-soft by design: an audit hiccup is logged and NEVER fails the write it
was recording. The hook is skipped entirely when no PAT is configured, and in
the unit suite (``PYTEST_CURRENT_TEST``) unless ``REPO_ACCESS_AUDIT_FORCE`` is
set -- so hermetic tests never touch the network.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("autopilot.repo_access_audit")

LEDGER_REPO = "ecosystem_change_logs"
AUDIT_PATH = "repo_access"
FEED_PATH = "repo_access/manifest.json"
_AGENT = "sophia"
_KNOWN_RESULTS = {"success", "failure"}


def _enabled() -> bool:
    """Audit only when a PAT exists and we're not in a hermetic test run."""
    if not settings.github_pat:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(
        "REPO_ACCESS_AUDIT_FORCE"
    ):
        return False
    return True


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_pat:
        h["Authorization"] = f"Bearer {settings.github_pat}"
    return h


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso_utc() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "record"


def _api_url(path: str) -> str:
    return f"https://api.github.com/repos/TrueSightDAO/{LEDGER_REPO}/contents/{path}"


def _read_file(path: str) -> dict[str, Any] | None:
    """Read a JSON file from the ledger repo; None on error/missing."""
    try:
        resp = httpx.get(_api_url(path), headers=_headers(), timeout=15.0)
        if resp.status_code != 200 or not isinstance(resp.json(), dict):
            return None
        raw = resp.json().get("content", "")
        if raw:
            decoded = base64.b64decode(raw).decode("utf-8", errors="replace")
            try:
                return json.loads(decoded)
            except Exception:
                return None
        return None
    except Exception as e:
        logger.warning("repo_access_audit: read %s failed: %s", path, e)
        return None


def _put_file(path: str, content: str, message: str) -> dict[str, Any]:
    """Create/update a file via the Contents API (fetches the blob sha first)."""
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": "main",
    }
    try:
        head = httpx.get(
            _api_url(path), headers=_headers(), params={"ref": "main"}, timeout=15.0
        )
        if head.status_code == 200 and isinstance(head.json(), dict):
            sha = head.json().get("sha")
            if sha:
                payload["sha"] = sha
        resp = httpx.put(_api_url(path), headers=_headers(), json=payload, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "success",
            "content_url": data.get("content", {}).get("html_url", ""),
        }
    except httpx.HTTPStatusError as exc:
        return {
            "status": "error",
            "error": f"GitHub API {exc.response.status_code}: {exc.response.text[:200]}",
        }
    except httpx.RequestError as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}


def _prepend_feed(rec: dict[str, Any]) -> None:
    """Prepend one record to repo_access/manifest.json (newest first)."""
    manifest = _read_file(FEED_PATH)
    entries: list[dict[str, Any]] = []
    if isinstance(manifest, dict):
        entries = list(manifest.get("entries", []))
    entries.insert(0, rec)
    payload = {
        "total": len(entries),
        "updated_utc": _iso_utc(),
        "entries": entries[:200],
    }
    res = _put_file(
        FEED_PATH, json.dumps(payload, indent=2) + "\n", f"audit feed: +{rec['id']}"
    )
    if res.get("status") != "success":
        logger.warning("repo_access_audit: feed update failed: %s", res.get("error"))


def record_repo_access(
    *,
    repo: str,
    action: str,
    result: str = "success",
    evidence_url: str = "",
    detail: str = "",
) -> dict[str, Any]:
    """Append one repo-access audit record. Never raises; fail-soft.

    ``action`` is the write surface (git_push_changes, create_repo,
    upload_file_to_github, ...). Returns ``{'status': 'success'|'skipped'|'error'}``.
    """
    try:
        if not _enabled():
            return {"status": "skipped", "reason": "audit disabled"}
        if not repo:
            return {"status": "skipped", "reason": "no repo"}
        if result not in _KNOWN_RESULTS:
            result = "failure"
        mode = "strict" if repo in settings.strict_repos else "default-allow"
        rec_id = f"repo_access_{_utcnow()}_{_slugify(repo)}"
        rec = {
            "id": rec_id,
            "timestamp_utc": _iso_utc(),
            "agent": _AGENT,
            "repo": repo,
            "action": action,
            "result": result,
            "mode": mode,
            "evidence_url": evidence_url,
            "detail": detail,
        }
        res = _put_file(
            f"{AUDIT_PATH}/{rec_id}.json",
            json.dumps(rec, indent=2) + "\n",
            f"repo-access audit {rec_id}",
        )
        if res.get("status") != "success":
            return {"status": "error", "error": res.get("error", "write failed")}
        _prepend_feed(rec)
        logger.info("repo_access_audit: %s %s -> %s (%s)", action, repo, result, mode)
        return {"status": "success", "id": rec_id}
    except Exception as e:  # fail-soft: never break the write being audited
        logger.warning("repo_access_audit: record failed: %s", e)
        return {"status": "error", "error": str(e)}
