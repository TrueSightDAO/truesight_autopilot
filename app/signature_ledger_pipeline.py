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
