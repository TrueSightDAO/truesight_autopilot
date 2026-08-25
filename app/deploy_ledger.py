"""Shared deploy/push ledger integration (DEPLOY_PUSH_SOP Phase 2).

Implements the soft-lock lease + append-only record flow from
`agentic_ai_context/sops/DEPLOY_PUSH_SOP.md`, backed by
TrueSightDAO/ecosystem_change_logs/deploys/ (API-only repo — everything
goes through the GitHub Contents API, never a local clone).

Flow per class push:
  1. check_lease(target_type, target_id)  -> is a live lease held?
     - live lease  -> REFUSE (block the push)
     - no lease    -> proceed
     - API error   -> fail-open with a loud warning (never block a deploy
       on our inability to READ; but if we can prove a live lease, block)
  2. acquire_lease(...)                   -> write deploys/leases/L-<..>.json
  3. <the push happens>
  4. append_deploy_record(...)            -> entries md+json + feed rebuild
  5. close_lease(lease_id)                -> DELETE the lease file

Fail-soft by design: any ledger write failure is logged and returned as a
warning in the tool result — the push itself is not rolled back because the
ledger hiccuped. The one hard stop is a *proven live lease* on the target.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("autopilot.deploy_ledger")

LEDGER_REPO = "ecosystem_change_logs"
LEASES_PATH = "deploys/leases"
ENTRIES_PATH = "deploys/entries"
FEED_PATH = "deploys/feed/manifest.json"
TTL_MINUTES = 30
_AGENT = "sophia"

# Registered identities mirroring agentic_ai_context/agents/*.json (the
# ledger script's KNOWN_AGENTS) — the autopilot always signs as itself.
KNOWN_AGENTS = {
    "sophia",
    "bionpact",
    "envoy",
    "deep seek",
    "deepseek",
    "kimi",
    "claude",
}
KNOWN_RESULTS = {"success", "failure", "rolled-back", "aborted", "in-progress"}
KNOWN_TARGET_TYPES = {"clasp", "gas", "repo", "ec2", "prod-sync", "other"}


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
    import re

    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "record"


def _api_url(path: str) -> str:
    return f"https://api.github.com/repos/TrueSightDAO/{LEDGER_REPO}/contents/{path}"


def _read_dir(path: str) -> list[dict[str, Any]]:
    """List a directory in the ledger repo; [] on error/empty."""
    try:
        resp = httpx.get(_api_url(path), headers=_headers(), timeout=15.0)
        if resp.status_code == 200 and isinstance(resp.json(), list):
            return resp.json()
        return []
    except Exception as e:
        logger.warning("deploy_ledger: list %s failed: %s", path, e)
        return []


def _read_file(path: str) -> dict[str, Any] | None:
    """Read a JSON file from the ledger repo; None on error."""
    try:
        resp = httpx.get(_api_url(path), headers=_headers(), timeout=15.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        raw = data.get("content", "")
        if data.get("encoding") == "base64" and raw:
            decoded = base64.b64decode(raw).decode("utf-8", errors="replace")
            try:
                return json.loads(decoded)
            except Exception:
                return {"_raw": decoded}
        return None
    except Exception as e:
        logger.warning("deploy_ledger: read %s failed: %s", path, e)
        return None


def _put_file(path: str, content: str, message: str) -> dict[str, Any]:
    """Create/update a file via the Contents API. Returns dict with status."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {"message": message, "content": encoded, "branch": "main"}
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
            "commit_sha": data.get("commit", {}).get("sha", ""),
        }
    except httpx.HTTPStatusError as exc:
        return {
            "status": "error",
            "error": f"GitHub API {exc.response.status_code}: {exc.response.text[:300]}",
        }
    except httpx.RequestError as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}


def _delete_file(path: str, message: str) -> dict[str, Any]:
    """Delete a file via the Contents API."""
    try:
        head = httpx.get(
            _api_url(path), headers=_headers(), params={"ref": "main"}, timeout=15.0
        )
        if head.status_code != 200 or not isinstance(head.json(), dict):
            return {"status": "error", "error": f"cannot fetch sha for {path}"}
        sha = head.json().get("sha")
        if not sha:
            return {"status": "error", "error": f"no sha for {path}"}
        resp = httpx.delete(
            _api_url(path),
            headers=_headers(),
            json={"message": message, "sha": sha, "branch": "main"},
            timeout=15.0,
        )
        resp.raise_for_status()
        return {"status": "success"}
    except httpx.HTTPStatusError as exc:
        return {
            "status": "error",
            "error": f"GitHub API {exc.response.status_code}: {exc.response.text[:300]}",
        }
    except httpx.RequestError as exc:
        return {"status": "error", "error": f"Request failed: {exc}"}


def _is_live(lease: dict[str, Any]) -> bool:
    """A lease is live iff open (or no status) and started within TTL."""
    if lease.get("status") not in (None, "open"):
        return False
    started = lease.get("started_utc") or lease.get("started")
    if not started:
        return False
    try:
        t = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except Exception:
        return True  # can't parse -> assume live (conservative)
    age_min = (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    return age_min <= float(lease.get("ttl_minutes", TTL_MINUTES))


def check_lease(target_type: str, target_id: str) -> dict[str, Any]:
    """Return {'status': 'clear'|'blocked'|'error', ...}.

    'clear'   -> no live lease on this target, safe to push.
    'blocked' -> a live lease exists; DO NOT push.
    'error'   -> could not verify; fail-open (caller warns + proceeds).
    """
    if not settings.github_pat:
        return {
            "status": "error",
            "reason": "no TRUESIGHT_DAO_AUTOPILOT PAT configured",
        }
    files = _read_dir(LEASES_PATH)
    if not files:
        return {"status": "clear", "leases_checked": 0}
    live = []
    for f in files:
        if f.get("type") != "file" or not f.get("name", "").endswith(".json"):
            continue
        lease = _read_file(f"{LEASES_PATH}/{f['name']}")
        if not lease:
            continue
        if (
            lease.get("target_id") != target_id
            or lease.get("target_type") != target_type
        ):
            continue
        if _is_live(lease):
            live.append(
                {
                    "lease_id": lease.get("id", f["name"]),
                    "agent": lease.get("agent"),
                    "started_utc": lease.get("started_utc"),
                    "ttl_minutes": lease.get("ttl_minutes", TTL_MINUTES),
                }
            )
    if live:
        return {"status": "blocked", "leases": live}
    return {
        "status": "clear",
        "leases_checked": len([f for f in files if f.get("type") == "file"]),
    }


def acquire_lease(target_type: str, target_id: str, action: str) -> dict[str, Any]:
    """Write an open lease file. Returns {'status','lease_id',...}."""
    files = _read_dir(LEASES_PATH)
    seq = (
        len(
            [
                f
                for f in files
                if f.get("type") == "file" and f.get("name", "").startswith("L-")
            ]
        )
        + 1
    )
    lease_id = f"L-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq:02d}"
    lease = {
        "id": lease_id,
        "agent": _AGENT,
        "target_type": target_type,
        "target_id": target_id,
        "action": action,
        "started_utc": _iso_utc(),
        "ttl_minutes": TTL_MINUTES,
        "status": "open",
    }
    res = _put_file(
        f"{LEASES_PATH}/{lease_id}.json",
        json.dumps(lease, indent=2) + "\n",
        f"lease: {_AGENT} -> {target_type}/{target_id}",
    )
    if res.get("status") != "success":
        return {
            "status": "error",
            "error": res.get("error", "lease write failed"),
            "lease_id": lease_id,
        }
    logger.info("deploy_ledger: acquired %s on %s/%s", lease_id, target_type, target_id)
    return {"status": "success", "lease_id": lease_id, **res}


def close_lease(lease_id: str) -> dict[str, Any]:
    """Delete the lease file (the final record references it)."""
    if not lease_id:
        return {"status": "error", "error": "no lease_id to close"}
    res = _delete_file(f"{LEASES_PATH}/{lease_id}.json", f"close lease {lease_id}")
    if res.get("status") == "success":
        logger.info("deploy_ledger: closed lease %s", lease_id)
    else:
        logger.warning(
            "deploy_ledger: close lease %s failed: %s", lease_id, res.get("error")
        )
    return res


def append_deploy_record(
    *,
    agent: str,
    target_type: str,
    target_id: str,
    action: str,
    result: str,
    evidence_url: str = "",
    git_ref: str = "",
    lease_id: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Append one record (md + json) and rebuild the feed manifest.

    Returns {'status','record_id','evidence_url',...}. Fail-soft: never
    raises; errors come back in the dict.
    """
    if agent.lower() not in KNOWN_AGENTS:
        return {
            "status": "error",
            "error": f"agent '{agent}' not in {sorted(KNOWN_AGENTS)}",
        }
    if result not in KNOWN_RESULTS:
        return {"status": "error", "error": f"result '{result}' not in {KNOWN_RESULTS}"}
    if target_type not in KNOWN_TARGET_TYPES:
        return {
            "status": "error",
            "error": f"target_type '{target_type}' not in {KNOWN_TARGET_TYPES}",
        }
    if result == "success" and not evidence_url:
        return {"status": "error", "error": "result=success requires evidence_url"}

    rec_id = f"deploy_{_utcnow()}_{_slugify(target_id)}"
    rec = {
        "id": rec_id,
        "agent": agent,
        "timestamp_utc": _utcnow(),
        "target_type": target_type,
        "target_id": target_id,
        "action": action,
        "git_ref": git_ref,
        "result": result,
        "lease_id": lease_id,
        "evidence_url": evidence_url,
        "notes": notes,
    }
    md_text = (
        f"---\nid: {rec_id}\nagent: {agent}\ntimestamp_utc: {rec['timestamp_utc']}\n"
        f"target_type: {target_type}\ntarget_id: {target_id}\naction: {action}\n"
        f"git_ref: {git_ref}\nresult: {result}\nlease_id: {lease_id}\n"
        f"evidence_url: {evidence_url}\n---\n\n## Record\n\n"
        f"- **Agent:** {agent}\n- **Time (UTC):** {rec['timestamp_utc']}\n"
        f"- **Target:** {target_type} `{target_id}`\n- **Action:** {action}\n"
        f"- **Result:** {result}\n- **Git ref:** {git_ref or 'n/a'}\n"
        f"- **Evidence:** {evidence_url or 'n/a'}\n\n{notes}\n"
    )
    js_text = json.dumps(rec, indent=2) + "\n"

    md_res = _put_file(
        f"{ENTRIES_PATH}/{rec_id}.md", md_text, f"deploy record {rec_id}"
    )
    js_res = _put_file(
        f"{ENTRIES_PATH}/{rec_id}.json", js_text, f"deploy record {rec_id}"
    )
    if md_res.get("status") != "success" or js_res.get("status") != "success":
        err = md_res.get("error") or js_res.get("error") or "record write failed"
        return {"status": "error", "error": err, "record_id": rec_id}
    rebuild_feed()
    logger.info(
        "deploy_ledger: appended %s (%s -> %s/%s)",
        rec_id,
        agent,
        target_type,
        target_id,
    )
    return {
        "status": "success",
        "record_id": rec_id,
        "evidence_url": md_res.get("content_url", ""),
        "content_url": md_res.get("content_url", ""),
    }


def rebuild_feed() -> dict[str, Any]:
    """Rebuild deploys/feed/manifest.json from all entries (newest first)."""
    files = _read_dir(ENTRIES_PATH)
    jsons = sorted(
        [
            f["name"]
            for f in files
            if f.get("type") == "file" and f.get("name", "").endswith(".json")
        ]
    )
    rows = []
    for name in jsons:
        rec = _read_file(f"{ENTRIES_PATH}/{name}")
        if isinstance(rec, dict) and rec.get("id"):
            rows.append(rec)
    rows.sort(key=lambda r: r.get("timestamp_utc", ""), reverse=True)
    manifest = {"total": len(rows), "updated_utc": _iso_utc(), "entries": rows[:200]}
    res = _put_file(
        FEED_PATH,
        json.dumps(manifest, indent=2) + "\n",
        f"rebuild feed ({len(rows)} records)",
    )
    if res.get("status") != "success":
        logger.warning("deploy_ledger: feed rebuild failed: %s", res.get("error"))
    return {
        "status": "success" if res.get("status") == "success" else "error",
        "total": len(rows),
    }
