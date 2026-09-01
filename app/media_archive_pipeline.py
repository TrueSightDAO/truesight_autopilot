"""Media Archives Pipeline dashboard — read-only data endpoint.

Serves queue state for the MAP (Media Archives Pipeline): per-farm counts and
items (uploaded / pending / needs_metadata / error), upload events, and the
committed manifest index. Auth-gated: requires a valid governor JWT.
"""

from __future__ import annotations

import json
import os
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

INBOX_ROOT = Path(
    os.environ.get("MEDIA_ARCHIVE_INBOX", "/home/ubuntu/media_archive_inbox")
)
UPLOAD_LOG = Path(
    os.environ.get("FARM_MEDIA_UPLOAD_LOG", "/tmp/farm_media_uploads.log")
)
MANIFEST_INDEX_URL = "https://raw.githubusercontent.com/TrueSightDAO/agentic_ai_context/main/FARM_MEDIA_MANIFESTS/index.json"

VALID_SOURCES = {"farm-media", "event-media", "partner-media"}


def _parse_sidecar(path: Path) -> dict[str, Any]:
    """Parse a sidecar JSON defensively — never hard-crash on schema drift."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _status_of(sidecar: dict[str, Any]) -> str:
    yt_id = sidecar.get("yt_id")
    err = sidecar.get("error")
    if yt_id:
        return "uploaded"
    if err:
        return "error"
    # pending but metadata-incomplete
    required = ("sha256", "gps", "title")
    if any(not sidecar.get(k) for k in required):
        return "needs_metadata"
    return "pending"


def _scan_inbox() -> list[dict[str, Any]]:
    """Scan inbox root for <source>/<farm_id>/*.json sidecars."""
    farms: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not INBOX_ROOT.exists():
        return []
    for source_dir in sorted(p for p in INBOX_ROOT.iterdir() if p.is_dir()):
        source = source_dir.name
        if source not in VALID_SOURCES:
            continue
        for farm_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            farm_id = farm_dir.name
            key = (source, farm_id)
            farms.setdefault(key, [])
            for sidecar_path in sorted(farm_dir.glob("*.json")):
                sidecar = _parse_sidecar(sidecar_path)
                if not sidecar:
                    continue
                sidecar["_path"] = str(sidecar_path)
                farms[key].append(sidecar)
    result = []
    for (source, farm_id), items in sorted(farms.items()):
        counts = {"uploaded": 0, "pending": 0, "needs_metadata": 0, "error": 0}
        for it in items:
            counts[_status_of(it)] += 1
        result.append(
            {
                "source": source,
                "farm_id": farm_id,
                "counts": counts,
                "total": len(items),
                "items": items,
            }
        )
    return result


def _read_upload_log(limit: int = 200) -> list[dict[str, str]]:
    """Tail the upload log into structured events (defensive parse)."""
    events: list[dict[str, str]] = []
    if not UPLOAD_LOG.exists():
        return events
    try:
        lines = UPLOAD_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return events
    for line in lines[-limit:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        events.append(
            {
                "ts": f"{parts[0]} {parts[1]}",
                "farm_id": parts[2],
                "file": parts[3].rstrip(":"),
                "result": " ".join(parts[4:]),
            }
        )
    return events


def _fetch_manifest_index() -> dict[str, Any]:
    """Fetch the committed manifest index (GitHub) — defensive."""
    try:
        import urllib.request

        with urllib.request.urlopen(MANIFEST_INDEX_URL, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/media-archive-pipeline/data")
def media_archive_pipeline_data(request: Request) -> dict[str, Any]:
    """Auth-gated queue state for the MAP dashboard."""
    verify_jwt(request)  # raises 401 without a valid governor token
    try:
        farms = _scan_inbox()
        events = _read_upload_log()
        index = _fetch_manifest_index()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "farms": farms,
            "upload_events": events,
            "manifest_index": index,
        }
    except Exception as exc:  # never 500 with raw internals
        raise HTTPException(
            status_code=500, detail=f"pipeline data error: {exc}"
        ) from exc


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<title>Media Archives Pipeline — TrueSight DAO</title>
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
<div class="header"><h1>Media Archives Pipeline</h1><div class="identity"><a href="/">TrueSight DAO</a> &middot; Governors</div></div><div class="wrap">
  <div class="sub">Live queue state for the DAO&rsquo;s media archives &mdash; signed-in governors only</div>
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
<div class="footer">MAP &middot; Media Archives Pipeline &middot; TrueSight DAO</div>
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
    let r = await fetch('/media-archive-pipeline/data', { credentials: 'same-origin' });
    if(r.status === 401){
      // 2. shared token as Bearer
      if(TOKEN){ r = await fetch('/media-archive-pipeline/data', { credentials: 'same-origin', headers: { 'Authorization': 'Bearer ' + TOKEN } }); }
      if(r.status === 401){ login.style.display='block'; meta.textContent='Session expired or invalid — log in again.'; TOKEN=''; localStorage.removeItem(SOPHIA_TOKEN_KEY); return; }
    }
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    meta.textContent = 'Generated ' + new Date(d.generated_at).toLocaleString();
    render(d);
  }catch(e){ err.style.display='block'; err.textContent = 'Error: ' + e.message; meta.textContent=''; }
}

function ytLink(id){ return id ? '<a href="https://www.youtube.com/watch?v=' + esc(id) + '" target="_blank">' + esc(id) + '</a>' : '—'; }

function render(d){
  const content = document.getElementById('content');
  let html = '';
  const farms = d.farms || [];
  if(!farms.length){ html += '<div class="card"><h2>Queue</h2><p>No farms in the inbox yet.</p></div>'; }
  for(const f of farms){
    const c = f.counts || {};
    const items = (f.items || []).slice().sort((a,b)=>(a.yt_id?0:1)-(b.yt_id?0:1));
    html += '<div class="card"><h2>' + esc(f.source + ' / ' + f.farm_id) + ' <span style="font-weight:400;font-size:0.8rem">(' + f.total + ' items)</span></h2>';
    html += '<p style="margin-bottom:0.6rem">';
    for(const [k,v] of Object.entries(c)){ html += '<span class="badge b-' + k + '" style="margin-right:0.3rem">' + k + ': ' + v + '</span>'; }
    html += '</p>';
    if(items.length){
      html += '<table><tr><th>File</th><th>Status</th><th>YouTube</th><th>GPS</th><th>Duration</th></tr>';
      for(const it of items){
        const st = it.yt_id ? 'uploaded' : (it.error ? 'error' : (!it.sha256 || !it.gps || !it.title ? 'needs_metadata' : 'pending'));
        const dur = it.duration ? (typeof it.duration === 'number' ? it.duration.toFixed(1)+'s' : esc(it.duration)) : '—';
        html += '<tr><td>' + esc(it.file || it.mp4 || it.filename || '?') + '</td>'
             + '<td><span class="badge b-' + st + '">' + st + '</span></td>'
             + '<td>' + ytLink(it.yt_id) + (it.error ? '<div style="color:#a32d2d;font-size:0.75rem">' + esc(String(it.error).slice(0,80)) + '</div>' : '') + '</td>'
             + '<td>' + esc(typeof it.gps === 'string' ? it.gps : (it.gps ? JSON.stringify(it.gps).slice(0,60) : '—')) + '</td>'
             + '<td>' + dur + '</td></tr>';
      }
      html += '</table>';
    }
    html += '</div>';
  }
  const ev = d.upload_events || [];
  if(ev.length){
    html += '<div class="card"><h2>Recent upload events</h2><div class="events"><table><tr><th>Time (UTC)</th><th>Farm</th><th>File</th><th>Result</th></tr>';
    for(const e of ev.slice(-60).reverse()){
      html += '<tr><td>' + esc(e.ts) + '</td><td>' + esc(e.farm_id) + '</td><td>' + esc(e.file) + '</td><td>' + esc(e.result) + '</td></tr>';
    }
    html += '</table></div></div>';
  }
  const mi = d.manifest_index || {};
  const idx = mi.index || mi.farms || [];
  if(Array.isArray(idx) && idx.length){
    html += '<div class="card"><h2>Committed manifests (GitHub)</h2><ul style="padding-left:1.2rem;font-size:0.85rem">';
    for(const m of idx){ html += '<li>' + esc(typeof m === 'string' ? m : JSON.stringify(m)) + '</li>'; }
    html += '</ul></div>';
  }
  content.innerHTML = html;
}

load();
</script>
</body>
</html>
"""


@router.get("/media-archive-pipeline", response_class=HTMLResponse)
def media_archive_pipeline_page(request: Request) -> HTMLResponse:
    """The MAP dashboard page — vault-style, cookie session (governor_chat_session)."""
    identity = _optional_identity(request)
    return _templates.TemplateResponse(
        request,
        "media_archive_pipeline.html",
        {"request": request, "identity": identity},
    )
