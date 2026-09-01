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
<link rel="icon" type="image/png" href="data:image/png;base64,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">
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
const SOPHIA_TOKEN_KEY = 'sophia_token'; // shared across governor-gated pages
let TOKEN = localStorage.getItem(SOPHIA_TOKEN_KEY) || '';

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function setToken(){
  TOKEN = document.getElementById('tok').value.trim();
  localStorage.setItem(SOPHIA_TOKEN_KEY, TOKEN);
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
