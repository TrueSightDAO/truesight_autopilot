"""Signature Ledger Pipeline dashboard — read-only data endpoint.

Serves live queue state for the public RSA attestation ledger
(TrueSightDAO/verify_public_signatures): per-event-type folder counts
(published / pending), backfill progress (files remaining + cursor), recent
cron pass activity, and the ledger index. Auth-gated: requires a valid
governor JWT. Data itself is public; this is an ops convenience layer.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .auth import verify_jwt
from .vault_routes import _optional_identity
from fastapi.templating import Jinja2Templates

_templates_dir = Path(__file__).resolve().parent / "templates" / "vault"
_templates = Jinja2Templates(directory=str(_templates_dir))

router = APIRouter()

LEDGER_REPO = "TrueSightDAO/verify_public_signatures"
TREE_API = f"https://api.github.com/repos/{LEDGER_REPO}/git/trees/main?recursive=1"
RAW_ROOT = f"https://raw.githubusercontent.com/{LEDGER_REPO}/main"

LOG_PATH = Path(
    os.environ.get(
        "SIGNATURE_LEDGER_LOG", "/home/ubuntu/scripts/sync_sunmint_signatures.log"
    )
)
CURSOR_PATH = Path(
    os.environ.get("SIGNATURE_LEDGER_CURSOR", "/home/ubuntu/scripts/.ledger_cursor")
)


def _fetch_github_tree() -> dict[str, Any]:
    """Fetch the ledger repo's recursive tree — defensive."""
    try:
        req = urllib.request.Request(TREE_API, headers={"User-Agent": "sophia"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _folder_stats(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Aggregate per-folder file counts from the git tree."""
    folders: dict[str, int] = {}
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        etype = entry.get("type")
        if etype != "blob" or "/" not in path:
            continue
        folder, name = path.split("/", 1)
        if name == "index.json":
            continue
        folders[folder] = folders.get(folder, 0) + 1
    return [
        {"folder": k, "published": v}
        for k, v in sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _read_log_tail(limit: int = 120) -> list[dict[str, str]]:
    """Tail the cron log into structured pass events (defensive parse)."""
    events: list[dict[str, str]] = []
    if not LOG_PATH.exists():
        return events
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return events
    for line in lines[-limit:]:
        if not line.strip():
            continue
        events.append({"line": line.strip()[:400]})
    return events


def _backfill_progress() -> dict[str, Any]:
    """Cursor + remain-count from the log (last rate-limit guard line)."""
    remain = None
    complete = False
    if LOG_PATH.exists():
        try:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []
        for line in reversed(lines):
            if "files remain for next cron pass" in line:
                import re as _re

                m = _re.search(r"(\d+) files remain", line)
                remain = int(m.group(1)) if m else None
                break
            if "backfill complete" in line:
                complete = True
                break
    cursor = ""
    try:
        cursor = CURSOR_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        cursor = ""
    return {
        "files_remaining": remain,
        "backfill_complete": complete,
        "cursor": cursor,
    }


@router.get("/signature-ledger-pipeline/data")
def signature_ledger_pipeline_data(request: Request) -> dict[str, Any]:
    """Auth-gated queue state for the signature ledger dashboard."""
    verify_jwt(request)  # raises 401 without a valid governor token
    try:
        tree = _fetch_github_tree()
        folders = _folder_stats(tree)
        log_events = _read_log_tail()
        progress = _backfill_progress()
        total_published = sum(f["published"] for f in folders)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ledger_repo": LEDGER_REPO,
            "raw_root": RAW_ROOT,
            "total_published": total_published,
            "folders": folders,
            "progress": progress,
            "log_events": log_events,
        }
    except Exception as exc:  # never 500 with raw internals
        raise HTTPException(
            status_code=500, detail=f"ledger pipeline data error: {exc}"
        ) from exc


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Signature Ledger Pipeline — TrueSight DAO</title>
<style>
  :root {
    --saffron: #e8a317; --saffron-light: #f5d78e; --saffron-dark: #c4890a;
    --bg: #faf8f5; --card-bg: #ffffff; --text: #2c2c2c; --text-muted: #6b6b6b;
    --border: #e0d8cc; --danger: #c0392b; --success: #27ae60;
    --font: 'Helvetica Neue', Helvetica, Arial, sans-serif;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: var(--font); background: var(--bg); color: var(--text); line-height: 1.6; min-height: 100vh; }
  .header { background: linear-gradient(135deg, var(--saffron-dark), var(--saffron)); color: white; padding: 1.2rem 2rem; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 1.4rem; font-weight: 600; }
  .header .identity { font-size: 0.85rem; opacity: 0.9; }
  .header a { color: white; text-decoration: none; }
  .header a:hover { text-decoration: underline; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 2rem 1rem; }
  .sub { color: var(--text-muted); margin-bottom: 1.2rem; font-size: 0.9rem; }
  .meta { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1.2rem; }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.2rem 1.4rem; margin-bottom: 1.2rem; overflow-x: auto; }
  .card h2 { font-size: 1.1rem; color: var(--saffron-dark); margin-bottom: 0.6rem; border-bottom: 1px solid var(--border); padding-bottom: 0.4rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--text-muted); font-weight: 600; }
  .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
  .b-done, .b-uploaded { background: #e8f5e9; color: #2e7d32; }
  .b-running, .b-pending { background: #fff8e1; color: #8a6a1e; }
  .b-pending { background: #f0e8da; color: #6a5a3a; }
  .b-needs_metadata { background: #f0e8da; color: #6a5a3a; }
  .b-error { background: #fde8e8; color: var(--danger); }
  .events { max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.78rem; }
  .err { color: var(--danger); padding: 1rem; background: #fde8e8; border-radius: 8px; margin-bottom: 1rem; }
  .login { text-align: center; padding: 3rem 1rem; }
  .login p { margin-bottom: 1rem; color: var(--text-muted); }
  a { color: #9e7340; }
  .footer { margin-top: 2rem; font-size: 0.8rem; color: var(--saffron-dark); text-align: center; }
</style>
</head>
<body>
<div class="header"><h1>Signature Ledger Pipeline</h1><div class="identity"><a href="/">TrueSight DAO</a> &middot; Governors</div></div><div class="wrap">
  <div class="sub">Live queue state for the public RSA attestation ledger &mdash; signed-in governors only</div>
  <div class="meta" id="meta">Loading&hellip;</div>
  <div id="login" class="card login" style="display:none">
    <p>You need to be a signed-in governor to view the pipeline.</p>
    <p><a href="/">Go to the Sophia landing page</a> and sign in, or paste your session token below.</p>
    <input id="tok" type="password" placeholder="JWT token" style="padding:0.5rem;width:60%;max-width:420px;border:1px solid #e4d5bf;border-radius:6px;font-family:inherit">
    <button onclick="setToken()" style="margin-top:0.6rem;padding:0.5rem 1.5rem;background:#b9894c;color:#f9f4ee;border:none;border-radius:20px;font-family:inherit;cursor:pointer">View pipeline</button>
  </div>
  <div id="err" class="err" style="display:none"></div>
  <div id="content"></div>
  </div>
<div class="footer">SLP &middot; Signature Ledger Pipeline &middot; TrueSight DAO</div>
</div>
<script>
let TOKEN = localStorage.getItem('slp_token') || '';

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function setToken(){
  TOKEN = document.getElementById('tok').value.trim();
  localStorage.setItem('slp_token', TOKEN);
  load();
}

async function load(){
  const meta = document.getElementById('meta');
  const err = document.getElementById('err');
  const content = document.getElementById('content');
  const login = document.getElementById('login');
  err.style.display = 'none';
  login.style.display = 'none';
  meta.textContent = 'Loading…';
  try{
    // 1. cookie-first (vault session carries over; verify_jwt falls back to governor_chat_session)
    let r = await fetch('/signature-ledger-pipeline/data', { credentials: 'same-origin' });
    if(r.status === 401){
      // 2. shared token as Bearer
      if(TOKEN){ r = await fetch('/signature-ledger-pipeline/data', { credentials: 'same-origin', headers: { 'Authorization': 'Bearer ' + TOKEN } }); }
      if(r.status === 401){ login.style.display='block'; meta.textContent='Session expired or invalid — log in again.'; TOKEN=''; localStorage.removeItem(SOPHIA_TOKEN_KEY); return; }
    }
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    meta.textContent = 'Generated ' + new Date(d.generated_at).toLocaleString() + ' · ' + d.total_published + ' attestations published across ' + d.folders.length + ' event types';
    render(d);
  }catch(e){ err.style.display='block'; err.textContent = 'Error: ' + e.message; meta.textContent=''; }
}

function render(d){
  const content = document.getElementById('content');
  let html = '';
  const p = d.progress || {};
  html += '<div class="card"><h2>Backfill status</h2><p style="margin-bottom:0.6rem">';
  if(p.backfill_complete){ html += '<span class="badge b-done">complete</span>'; }
  else if(p.files_remaining != null){ html += '<span class="badge b-running">running</span> ' + esc(String(p.files_remaining)) + ' files remain (~' + Math.ceil(p.files_remaining/250) + ' cron passes)'; }
  else { html += '<span class="badge b-pending">pending</span>'; }
  html += '</p>';
  if(p.cursor){ html += '<p style="font-size:0.8rem;color:#8a6a45">cursor: ' + esc(p.cursor) + '</p>'; }
  html += '<p style="font-size:0.8rem;color:#8a6a45">repo: <a href="https://github.com/' + esc(d.ledger_repo) + '" target="_blank">' + esc(d.ledger_repo) + '</a></p>';
  html += '</div>';
  const folders = d.folders || [];
  html += '<div class="card"><h2>Event-type folders (' + folders.length + ')</h2>';
  if(folders.length){
    html += '<table><tr><th>Folder</th><th>Published</th><th>Link</th></tr>';
    for(const f of folders){
      html += '<tr><td>' + esc(f.folder) + '</td><td>' + esc(String(f.published)) + '</td>'
           + '<td><a href="' + esc(d.raw_root + '/' + f.folder + '/index.json') + '" target="_blank">index</a></td></tr>';
    }
    html += '</table>';
  } else { html += '<p>No folders yet — backfill may still be starting.</p>'; }
  html += '</div>';
  const ev = d.log_events || [];
  if(ev.length){
    html += '<div class="card"><h2>Recent cron activity</h2><div class="events">';
    for(const e of ev){ html += '<div>' + esc(e.line) + '</div>'; }
    html += '</div></div>';
  }
  content.innerHTML = html;
}

load();
</script>
</body>
</html>
"""


@router.get("/signature-ledger-pipeline", response_class=HTMLResponse)
def signature_ledger_pipeline_page(request: Request) -> HTMLResponse:
    """The Signature Ledger Pipeline page — vault-style, cookie session."""
    identity = _optional_identity(request)
    return _templates.TemplateResponse(
        request,
        "signature_ledger_pipeline.html",
        {"request": request, "identity": identity},
    )
