"""Visual UAT — Playwright captures + Gemini judgement, as ONE tool.

Why this exists
---------------
Visual acceptance testing used to be hand-rolled per task: launch a browser,
screenshot, eyeball it, ask a vision model, then *manually* work out whether
the model's complaint was real. That hand-roll produced two failure modes:

1. **The model was treated as the verdict.** A judge model is good at "something
   looks wrong here" and unreliable at "what exactly is wrong". In practice it
   reported a cart button protruding from the header (it was an off-canvas
   panel) and "no dark scrim" (a 0.6-0.7 gradient did exist).
2. **A naive broken-image check reports zero.** Images hidden via
   ``onerror="this.style.display='none'"`` stay ``complete`` with a natural
   width, so they never count as "broken" — the page just silently loses them.

So this tool returns TWO CHANNELS and never mixes them:

- ``dom_facts`` / ``assertions`` — programmatic ground truth. **These alone
  decide ``verdict``.**
- ``visual_findings`` — the model's prose, every item tagged
  ``"verified": false``. It can never turn a pass into a fail. Callers must
  probe a finding before repeating it as fact.

Usage
-----
    visual_uat(url="https://beta.agroverse.shop/cacao-journeys/",
               expectations={"gathering-venues-count": "27",
                             "brazilian-farms-count": "28"})

``expectations`` keys are either a bare DOM id (most of our stats carry an id)
or a CSS selector; values are the expected trimmed ``textContent``.

Credentials: ``GEMINI_API_KEY``. The vision model is resolved from the live
model list at call time — never hardcoded, because retired names return
``404`` for new keys (``gemini-2.5-flash`` -> ``gemini-3.8-flash`` happened
mid-project and silently broke a hand-rolled harness).

Degrades cleanly: no playwright / no browser / no API key returns
``status: "error"`` with a fixable reason, not a crash.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("autopilot.tools.visual_uat")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT = 60
_DEFAULT_VIEWPORTS = [
    {"label": "desktop", "width": 1440, "height": 900},
    {"label": "mobile", "width": 390, "height": 844},
]

# Model preference order when resolving from the live list. Anything with
# "flash" is preferred (cheap + vision-capable); image-generation models are
# excluded because they don't return prose.
# ``supportedGenerationMethods`` is NOT a trustworthy capability signal:
# ``gemini-omni-flash-preview`` advertises ``generateContent`` and then rejects
# with "This model only supports Interactions API." So every candidate is
# PROBED before use, and anything that advertises prose-incompatible output is
# filtered out here first.
_MODEL_DENY = (
    "image",
    "imagen",
    "banana",  # nano-banana-* = image generation
    "tts",
    "embedding",
    "native-audio",
    "veo",
    "lyria",
    "omni",  # Interactions API only
    "deep-research",
    "customtools",
)


def _err(reason: str, **extra: Any) -> str:
    return json.dumps({"status": "error", "reason": reason, **extra})


# ── pure helpers (unit-tested, no browser / no network) ──────────────────────


def _norm(text: Any) -> str:
    """Normalise text for comparison: collapse whitespace, unify dashes.

    Stats render as ``~1,597`` or ``—`` / ``&mdash;``; comparing raw strings
    produces false failures over an en-dash vs em-dash.
    """
    if text is None:
        return ""
    s = str(text).replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
    s = s.replace("&mdash;", "-").replace("&ndash;", "-")
    return re.sub(r"\s+", " ", s).strip()


def evaluate_assertions(
    facts: dict[str, Any], expectations: dict[str, str] | None
) -> list[dict[str, Any]]:
    """Compare extracted values against expectations. Pure.

    ``facts`` maps expectation-key -> extracted text (already per-viewport).
    Returns one row per expectation so a caller sees *both* what was wanted and
    what was found, never just "failed".
    """
    rows: list[dict[str, Any]] = []
    for key, expected in (expectations or {}).items():
        actual = facts.get(key)
        ok = actual is not None and _norm(actual) == _norm(expected)
        rows.append(
            {
                "key": key,
                "expected": expected,
                "actual": actual,
                "pass": ok,
            }
        )
    return rows


def find_collapsed(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag elements that have content but render at (near) zero height.

    This is the check that catches the ``onerror -> display:none`` family of
    bug: the element still exists in the DOM, still has text/pixels, but is
    visually gone. A ``naturalWidth === 0`` image check misses it entirely.
    """
    out = []
    for el in elements or []:
        height = el.get("height") or 0
        content_len = el.get("content_len") or 0
        if content_len > 0 and height <= 1:
            out.append(
                {
                    "selector": el.get("selector"),
                    "content_len": content_len,
                    "height": height,
                    "sample": (el.get("text") or "")[:60],
                }
            )
    return out


def rank_vision_models(payload: dict[str, Any], limit: int = 6) -> list[str]:
    """Ordered shortlist of plausible text/vision models from ``ListModels``.

    Pure so the ranking is testable without the network. Returns a LIST, not one
    name, because the advertised capability metadata cannot be trusted — the
    caller probes candidates in order until one actually answers.
    """
    scored: list[tuple[tuple[float, int, int], str]] = []
    for m in payload.get("models", []) or []:
        name = str(m.get("name", "")).replace("models/", "")
        if not name:
            continue
        methods = m.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        low = name.lower()
        if any(bad in low for bad in _MODEL_DENY):
            continue
        if "flash" not in low and "pro" not in low:
            continue
        ver = re.search(r"(\d+(?:\.\d+)?)", name)
        # Higher version first, then non-lite, then flash (cheaper) over pro.
        scored.append(
            (
                (
                    float(ver.group(1)) if ver else 0.0,
                    0 if "lite" in low else 1,
                    1 if "flash" in low else 0,
                ),
                name,
            )
        )
    scored.sort(reverse=True)
    return [name for _score, name in scored[:limit]]


def pick_vision_model(payload: dict[str, Any]) -> str | None:
    """Backwards-compatible single pick: best ranked candidate, unprobed."""
    ranked = rank_vision_models(payload, limit=1)
    return ranked[0] if ranked else None


def parse_findings(text: str, max_findings: int = 8) -> list[dict[str, Any]]:
    """Turn the model's prose into structured, explicitly-UNVERIFIED findings.

    Line-oriented on purpose: the model returns prose, and pretending we can
    reliably impose a JSON contract on it is how these harnesses rot.
    """
    findings: list[dict[str, Any]] = []
    if not text:
        return findings
    cleaned = re.sub(r"[*_`]", "", text)
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if len(line) < 25:
            continue
        if re.match(
            r"^(based on|here are|overall|summary|in summary|the desktop layout|note:)",
            line,
            re.IGNORECASE,
        ):
            continue
        text_out = re.sub(r"^\d+[.)]\s*", "", line)
        if len(text_out) < 25:
            continue
        findings.append(
            {"finding": text_out[:400], "severity": "unknown", "verified": False}
        )
        if len(findings) >= max_findings:
            break
    return findings


_REQUIRED_MODES = ("desktop", "mobile")

_ANALYTICS_HOSTS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "connect.facebook.net",
    "hotjar.com",
    "segment.io",
    "mixpanel.com",
    "clarity.ms",
    "nr-data.net",
)

_PAGE_ASSET_EXTS = (
    ".js",
    ".mjs",
    ".css",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".svg",
    ".ico",
    ".json",
    ".woff",
    ".woff2",
    ".ttf",
)


def _host(u: str) -> str:
    """Host portion of a URL without importing urllib (kept dependency-free)."""
    parts = (u or "").split("://", 1)
    if len(parts) != 2:
        return ""
    return parts[1].split("/", 1)[0].split("?", 1)[0].lower()


def _origin(u: str) -> str:
    """scheme://host of a URL, or '' when the URL is relative/garbage."""
    parts = (u or "").split("://", 1)
    if len(parts) != 2:
        return ""
    return (parts[0] + "://" + parts[1].split("/", 1)[0]).lower()


def missing_required_modes(viewports: list[dict[str, Any]]) -> list[str]:
    """Which of the required desktop/mobile modes this run did NOT render."""
    labels = {str(vp.get("label", "")) for vp in viewports}
    return [m for m in _REQUIRED_MODES if m not in labels]


def classify_request_failures(
    failed: list[dict[str, Any]], page_url: str
) -> tuple[list[str], list[str], list[str]]:
    """Split request failures into (page, third_party, analytics).

    ONLY ``page`` may affect the verdict. Analytics beacons and third-party
    origins are reported for visibility but must never fail a page the site
    itself renders correctly -- GA4 aborts its ``collect`` call from headless
    Chromium, which made the verdict flip between identical runs.
    """
    page_origin = _origin(page_url)
    page: list[str] = []
    third: list[str] = []
    analytics: list[str] = []
    for item in failed:
        url = str(item.get("url", "") or "")
        label = str(item.get("viewport", "") or "")
        host = _host(url)
        leaf = url.split("?")[0].rsplit("/", 1)[-1] or url[:60]
        line = f"[{label}] {leaf}"
        if host and any(pat in host for pat in _ANALYTICS_HOSTS):
            analytics.append(f"{line}  ({host})")
        elif _origin(url) and _origin(url) != page_origin:
            third.append(f"{line}  ({host})")
        elif not host:
            page.append(f"{line}  (page-relative)")
        else:
            page.append(f"{line}  ({host})")
    return page, third, analytics


def is_page_asset(url: str) -> bool:
    """True when a vanished same-origin request is a real page asset."""
    path = (url or "").split("?")[0].split("#")[0].lower()
    return path.endswith(_PAGE_ASSET_EXTS)


def build_verdict(
    assertions: list[dict[str, Any]],
    console_errors: list[str],
    failed_requests: list[str],
) -> str:
    """Verdict is decided by ground truth ONLY — never by model prose."""
    if any(not a["pass"] for a in assertions):
        return "fail"
    if console_errors or failed_requests:
        return "fail"
    return "pass"


# ── live pieces ─────────────────────────────────────────────────────────────


_RESOLVED_MODEL: str | None = None


def _probe(api_key: str, model: str) -> bool:
    """Does this model actually accept a plain text generateContent call?"""
    try:
        r = requests.post(
            f"{_GEMINI_BASE}/models/{model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [{"parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 5},
            },
            timeout=25,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _resolve_model(api_key: str) -> tuple[str | None, str]:
    """Resolve a working model: env override, else probe the ranked shortlist.

    Never hardcodes a model name — retired names (``gemini-2.5-flash``) return
    404 for newer keys and silently break a harness that pins one.
    """
    global _RESOLVED_MODEL
    if _RESOLVED_MODEL:
        return _RESOLVED_MODEL, "cached"

    env = os.environ.get("GEMINI_VISION_MODEL", "").strip()
    if env:
        _RESOLVED_MODEL = env
        return env, "env"

    try:
        r = requests.get(
            f"{_GEMINI_BASE}/models", headers={"x-goog-api-key": api_key}, timeout=30
        )
    except requests.RequestException as e:
        logger.warning("visual_uat: model list failed: %s", e)
        return None, "unresolved"

    if r.status_code != 200:
        return None, f"model list HTTP {r.status_code}"

    ranked = rank_vision_models(r.json())
    for candidate in ranked:
        if _probe(api_key, candidate):
            _RESOLVED_MODEL = candidate
            logger.info("visual_uat: resolved vision model %s (probed)", candidate)
            return candidate, "probed"

    return None, f"none of {len(ranked)} candidates answered a probe"


def _judge(
    api_key: str, model: str, prompt: str, shots: list[tuple[str, str]]
) -> tuple[str, str]:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for label, path in shots:
        parts.append({"text": f"[{label} viewport]"})
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": base64.b64encode(Path(path).read_bytes()).decode(),
                }
            }
        )
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1500},
    }
    resp = requests.post(
        f"{_GEMINI_BASE}/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json=body,
        timeout=120,
    )
    if resp.status_code != 200:
        return "", f"HTTP {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"], ""
    except (KeyError, IndexError):
        return "", f"unexpected response: {json.dumps(data)[:300]}"


_PROMPT = (
    "You are reviewing screenshots of the SAME page at desktop and mobile widths for "
    "visual acceptance testing. Report ONLY concrete visual defects you can actually "
    "see: overlapping text, clipped or cut-off text, unreadable contrast, missing or "
    "broken images, misaligned or overflowing elements, crushed buttons. "
    "One defect per line. If the page looks clean, say 'Page looks clean' plainly. "
    "Do not speculate about code or causes — describe only what is visible."
)


def visual_uat(
    url: str,
    expectations: dict[str, str] | None = None,
    viewports: list[dict[str, Any]] | None = None,
    judge: bool = True,
    full_page: bool = False,
    wait_ms: int = 2000,
) -> str:
    if not url:
        return _err("url is required")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _err(
            "playwright not installed",
            fix="pip install playwright==1.63.0 && playwright install chromium",
        )

    vps = viewports or _DEFAULT_VIEWPORTS
    expectations = expectations or {}
    shot_dir = Path(tempfile.mkdtemp(prefix="visual_uat_"))

    dom_facts: dict[str, Any] = {}
    all_assertions: list[dict[str, Any]] = []
    shots: list[tuple[str, str]] = []
    console_errors: list[str] = []
    failed_requests: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            for vp in vps:
                label = vp.get("label", f"{vp.get('width')}x{vp.get('height')}")
                page = browser.new_page(
                    viewport={
                        "width": vp.get("width", 1440),
                        "height": vp.get("height", 900),
                    },
                    device_scale_factor=2,
                )
                page.on(
                    "console",
                    lambda m, _lbl=label: (
                        console_errors.append(f"[{_lbl}] {m.text[:160]}")
                        if m.type == "error"
                        else None
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda r, _lbl=label: failed_requests.append(
                        {
                            "viewport": _lbl,
                            "url": r.url,
                            "reason": str(getattr(r, "failure", "") or ""),
                        }
                    ),
                )
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(wait_ms)

                extracted = page.evaluate(
                    """(keys) => {
                        const out = {};
                        for (const k of keys) {
                            const sel = k.startsWith('#') || k.startsWith('.') || k.startsWith('[') ? k : '#' + k;
                            let el = document.querySelector(sel);
                            if (!el) { const byId = document.getElementById(k); el = byId; }
                            out[k] = el ? (el.textContent || '').trim() : null;
                        }
                        return out;
                    }""",
                    list(expectations.keys()),
                )

                collapsed_raw = page.evaluate(
                    """() => {
                        const out = [];
                        document.querySelectorAll('body *').forEach(e => {
                            const txt = (e.textContent || '').trim();
                            const rect = e.getBoundingClientRect();
                            const hasImg = e.tagName === 'IMG';
                            const contentLen = hasImg ? 1 : txt.length;
                            if (contentLen === 0) return;
                            if (e.children.length > 3 && !hasImg) return;
                            out.push({
                                selector: e.tagName.toLowerCase() + (e.id ? '#' + e.id : (e.className ? '.' + String(e.className).split(' ')[0] : '')),
                                content_len: contentLen,
                                height: Math.round(rect.height),
                                text: txt,
                            });
                        });
                        return out.slice(0, 400);
                    }"""
                )

                hidden_imgs = page.evaluate(
                    """() => Array.from(document.images).filter(i =>
                        getComputedStyle(i).display === 'none'
                        || getComputedStyle(i).visibility === 'hidden'
                        || (i.complete && i.naturalWidth === 0)).map(i => i.getAttribute('src') || '')"""
                )

                shot_path = str(shot_dir / f"{label}.png")
                page.screenshot(path=shot_path, full_page=full_page)
                shots.append((label, shot_path))

                rows = evaluate_assertions(extracted, expectations)
                for row in rows:
                    row["viewport"] = label
                all_assertions.extend(rows)

                dom_facts[label] = {
                    "values": extracted,
                    "broken_or_hidden_images": hidden_imgs,
                    "collapsed_elements": find_collapsed(collapsed_raw),
                    "screenshot": shot_path,
                }
                page.close()
            browser.close()
    except (RuntimeError, OSError, TimeoutError) as e:  # launch / navigation
        return _err(f"playwright run failed: {e}")

    findings: list[dict[str, Any]] = []
    judge_error = ""
    if judge:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            judge_error = "GEMINI_API_KEY not configured — DOM channel still valid"
        else:
            model, how = _resolve_model(api_key)
            if not model:
                judge_error = "no vision model resolved from the live model list"
            else:
                text, judge_error = _judge(api_key, model, _PROMPT, shots)
                findings = parse_findings(text)
                for f in findings:
                    f["model"] = model
                    f["model_source"] = how

    # Mode coverage is asserted, not assumed: desktop AND mobile must render.
    for _mode in missing_required_modes(vps):
        console_errors.append(f"[config] required viewport mode not rendered: {_mode}")

    page_failures, thirdparty_failures, analytics_failures = classify_request_failures(
        failed_requests, url
    )
    status = build_verdict(all_assertions, console_errors, page_failures)
    return json.dumps(
        {
            "status": "ok",
            "url": url,
            "verdict": status,
            "verdict_basis": (
                "dom_facts + console + same-origin page-request failures only "
                "(model prose, analytics beacons and third-party origins excluded)"
            ),
            "assertions": all_assertions,
            "console_errors": console_errors,
            "failed_requests": page_failures,
            "thirdparty_failed_requests": thirdparty_failures,
            "analytics_beacons": analytics_failures,
            "viewport_modes": [str(vp.get("label", "")) for vp in vps],
            "modes_checked": len(vps),
            "dom_facts": dom_facts,
            "visual_findings": findings,
            "visual_findings_note": (
                "UNVERIFIED model observations. Never fold these into a report as fact — "
                "probe the DOM/pixels first (a model has reported both a protruding button "
                "that was an off-canvas panel and a missing scrim that existed)."
            ),
            "judge_error": judge_error,
            "captured_at": int(time.time()),
        },
        indent=2,
    )


# ── capability manifest entry ───────────────────────────────────────────────

from ..tool_registry import ToolSpec

TOOL_SPEC = ToolSpec(
    name="visual_uat",
    description=(
        "Visual acceptance testing for a live page: renders it with Playwright at desktop + "
        "mobile, captures screenshots, asserts DOM values/console/failed-request/broken-image "
        "ground truth, and optionally has Gemini describe visible defects. Returns TWO "
        "separate channels: dom_facts+assertions (verified, decides pass/fail) and "
        "visual_findings (UNVERIFIED model prose, never affects the verdict — probe before "
        "reporting). Use this instead of hand-rolling screenshots + a vision call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to render (http/https).",
            },
            "expectations": {
                "type": "object",
                "description": 'Map of DOM id (or CSS selector) -> expected trimmed textContent, e.g. {"brazilian-farms-count": "28"}.',
            },
            "judge": {
                "type": "boolean",
                "description": "Ask Gemini for visual findings (default true). DOM channel works either way.",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture full-page screenshots instead of viewport only (default false).",
            },
            "wait_ms": {
                "type": "integer",
                "description": "Extra settle time after networkidle in ms (default 2000).",
            },
        },
        "required": ["url"],
    },
    handler=lambda args, ctx: visual_uat(
        url=args.get("url", ""),
        expectations=args.get("expectations"),
        judge=bool(args.get("judge", True)),
        full_page=bool(args.get("full_page", False)),
        wait_ms=int(args.get("wait_ms", 2000)),
    ),
)
