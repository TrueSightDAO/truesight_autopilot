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
<title>Media Archives Pipeline — TrueSight DAO</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #f9f4ee; color: #24160b; font-family: 'Georgia', 'Times New Roman', serif; padding: 2rem 1rem; }
  .wrap { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 1.6rem; letter-spacing: 0.03em; color: #24160b; margin-bottom: 0.2rem; }
  .sub { color: #b9894c; font-style: italic; margin-bottom: 1.5rem; font-size: 0.95rem; }
  .meta { font-size: 0.8rem; color: #8a6a45; margin-bottom: 1.5rem; }
  .card { background: #fff; border: 1px solid #e4d5bf; border-radius: 10px; padding: 1rem 1.2rem; margin-bottom: 1.2rem; }
  .card h2 { font-size: 1.1rem; color: #b9894c; margin-bottom: 0.6rem; border-bottom: 1px solid #efe4d2; padding-bottom: 0.4rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #f0e8da; vertical-align: top; }
  th { color: #8a6a45; font-weight: 600; }
  .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
  .b-uploaded { background: #e4f3e4; color: #2d6a2d; }
  .b-pending { background: #fdf3d7; color: #8a6a1e; }
  .b-needs_metadata { background: #f0e8da; color: #6a5a3a; }
  .b-error { background: #f9e0e0; color: #a32d2d; }
  .events { max-height: 300px; overflow-y: auto; }
  .err { color: #a32d2d; padding: 1rem; background: #fdf0f0; border-radius: 8px; margin-bottom: 1rem; }
  .login { text-align: center; padding: 3rem 1rem; }
  .login p { margin-bottom: 1rem; color: #8a6a45; }
  a { color: #9e7340; }
  .footer { margin-top: 2rem; font-size: 0.8rem; color: #b9894c; text-align: center; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Media Archives Pipeline</h1>
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
  <div class="footer">MAP &middot; Media Archives Pipeline &middot; TrueSight DAO</div>
</div>
<script>
let TOKEN = localStorage.getItem('map_token') || '';

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function setToken(){
  TOKEN = document.getElementById('tok').value.trim();
  localStorage.setItem('map_token', TOKEN);
  load();
}

async function load(){
  const meta = document.getElementById('meta');
  const err = document.getElementById('err');
  const content = document.getElementById('content');
  const login = document.getElementById('login');
  err.style.display = 'none';
  if(!TOKEN){ login.style.display='block'; meta.textContent='Signed out — log in to view.'; return; }
  login.style.display = 'none';
  meta.textContent = 'Loading…';
  try{
    const r = await fetch('/media-archive-pipeline/data', { headers: { 'Authorization': 'Bearer ' + TOKEN } });
    if(r.status === 401){ login.style.display='block'; meta.textContent='Session expired or invalid — log in again.'; TOKEN=''; localStorage.removeItem('map_token'); return; }
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
def media_archive_pipeline_page() -> HTMLResponse:
    """The MAP dashboard page (login prompt; data is auth-gated)."""
    return HTMLResponse(DASHBOARD_HTML)
