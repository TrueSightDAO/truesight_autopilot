"""farm_media_gallery — build a farm/program media gallery from MAP sidecars.

PR3 of the Media Gallery Publisher plan (thread 26438).  A *pure, idempotent*
transform: MAP sidecars (+ manifest v2 metadata) -> the gallery JSON the site
page fetches, matching the hand-authored
``agroverse_shop_beta/farms/<slug>/media.json`` contract read by
``js/media-gallery.js``::

    {"schemaVersion": 1, "collection_id": "<id>", "gallery": [ ...entries... ]}

Why this exists: the site gallery was the only *hand-authored* stage in the
Media Archives Pipeline, so "uploaded to YouTube" and "published on the page"
silently diverged (the Cacau na Veia PR6 near-miss: 33 videos uploaded, page
never wired).  Deriving the gallery from the sidecars makes that divergence
structurally impossible.

Design constraints:

* **Deterministic / idempotent.**  Entries are ordered by capture time (then
  file name) and serialised with fixed formatting, so re-running on unchanged
  inputs is byte-identical — safe for a systemd-timer reconcile (PR5).  No
  wall-clock field is emitted unless the caller supplies one.
* **Whisper QA guard.**  Hallucinated boilerplate subtitles ("Legendas pela
  comunidade de Amara.org", "Thanks for watching…") are dropped, so a 0.3 s
  silent clip never ships a spurious caption.
* **No side effects.**  No network, no repo writes.  Publishing to
  ``farm_media_manifests`` is a later unit (PR4/PR5).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

# --- Whisper hallucination guard -------------------------------------------
# Hard patterns: never part of real farm narration -> drop the whole caption.
_HARD_QA_PATTERNS = [
    "amara.org",
    "legendas pela comunidade",
    "legendado pela comunidade",
    "subtitles by the amara.org community",
    "subtitles by the community",
    "редактор субтитров",
    "продолжение следует",
]
# Soft patterns: YouTube boilerplate that *could* appear inside legitimate
# speech -> drop only when it effectively IS the whole caption.
_SOFT_QA_PATTERNS = [
    "thanks for watching",
    "thank you for watching",
    "obrigado por assistir",
    "obrigada por assistir",
    "obrigado por ver",
    "inscreva-se no canal",
    "se inscreva no canal",
    "curta e compartilhe",
]
_MIN_CAPTION_LEN = 3

_HARD_QA_RE = [re.compile(re.escape(p), re.IGNORECASE) for p in _HARD_QA_PATTERNS]
_SOFT_QA_RE = [re.compile(re.escape(p), re.IGNORECASE) for p in _SOFT_QA_PATTERNS]


def clean_caption(text: str | None) -> str:
    """Return a usable caption, or "" if the text is empty/boilerplate.

    Whitespace is collapsed; Whisper hallucinations are dropped (see module
    docstring).  Dropping a real-but-noisy caption is the intended trade-off —
    a spurious "Legendas pela comunidade de Amara.org" caption is worse than
    no caption at all.
    """
    if not text:
        return ""
    flat = " ".join(str(text).split()).strip()
    if len(flat) < _MIN_CAPTION_LEN:
        return ""
    if any(rx.search(flat) for rx in _HARD_QA_RE):
        return ""
    # Soft boilerplate: drop only when it constitutes the WHOLE caption,
    # so "Thanks for watching the drying racks..." survives intact.
    residual = flat
    for rx in _SOFT_QA_RE:
        residual = rx.sub(" ", residual)
    if not re.search(r"[A-Za-z0-9\u00c0-\u00ff]", residual):
        return ""
    return flat


# --- aspect -----------------------------------------------------------------


def aspect_from_dims(width: int | None, height: int | None) -> str:
    """'portrait' when taller than wide, else 'landscape' (media-gallery.js)."""
    try:
        w = int(width)  # type: ignore[arg-type]
        h = int(height)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "landscape"
    return "portrait" if h > w else "landscape"


def probe_aspect(path: str | Path) -> str | None:
    """Aspect from the first video stream via ffprobe.  None on any failure."""
    try:
        proc = subprocess.run(  # noqa: S603
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = (
        proc.stdout.strip().splitlines()[0].split(",") if proc.stdout.strip() else []
    )
    if len(parts) < 2:
        return None
    try:
        return aspect_from_dims(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


# --- loading ----------------------------------------------------------------


def _load_json(path: str | Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _stem(file_name: str | None, fallback: str = "") -> str:
    if not file_name:
        return fallback
    return Path(str(file_name)).stem or fallback


def _date_only(captured_at: str | None) -> str:
    """'2026-09-09T13:35:23+0000' -> '2026-09-09' (best effort)."""
    if not captured_at:
        return ""
    return str(captured_at).split("T", 1)[0].strip()


def gather_videos(inbox_dir: str | Path, *, probe: bool = True) -> list[dict[str, Any]]:
    """Read MAP sidecars from one inbox dir -> normalised video records.

    Only sidecars with a ``yt_id`` (i.e. uploaded) are returned.  ``probe``
    enables the ffprobe aspect lookup against the sibling ``.mp4``.
    """
    inbox = Path(inbox_dir)
    videos: list[dict[str, Any]] = []
    if not inbox.is_dir():
        return videos
    for sidecar_path in sorted(inbox.glob("*.json")):
        sidecar = _load_json(sidecar_path)
        if not isinstance(sidecar, dict) or not sidecar.get("yt_id"):
            continue
        mp4 = sidecar_path.with_suffix("")  # IMG_9493.mp4.json -> IMG_9493.mp4
        aspect = probe_aspect(mp4) if (probe and mp4.exists()) else None
        videos.append(
            {
                "file": sidecar.get("file") or mp4.name,
                "yt_id": sidecar.get("yt_id"),
                "duration_s": sidecar.get("duration_s"),
                "captured_at": sidecar.get("captured_at"),
                "transcription": sidecar.get("transcription"),
                "aspect": aspect,
            }
        )
    return videos


def videos_from_manifest(
    manifest: dict[str, Any] | None, *, probe_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """Fallback source: build video records from a manifest v2 ``items`` list.

    Used when inbox sidecars are gone but the committed manifest still carries
    ``yt_id`` per item (the manifest is the durable layer).  ``probe_dir``, if
    given, is used for the ffprobe aspect lookup by file stem.
    """
    if not isinstance(manifest, dict):
        return []
    probe_root = Path(probe_dir) if probe_dir else None
    videos: list[dict[str, Any]] = []
    for item in manifest.get("items") or []:
        if not isinstance(item, dict) or not item.get("yt_id"):
            continue
        aspect = None
        if probe_root is not None:
            stem = _stem(item.get("file"))
            for cand in (
                probe_root / f"{stem}.mp4",
                probe_root / str(item.get("file", "")),
            ):
                if cand.exists():
                    aspect = probe_aspect(cand)
                    break
        videos.append(
            {
                "file": item.get("file"),
                "yt_id": item.get("yt_id"),
                "duration_s": item.get("duration_s"),
                "captured_at": item.get("captured_at") or item.get("creation_date"),
                "transcription": item.get("transcription"),
                "aspect": aspect,
            }
        )
    return videos


# --- building ---------------------------------------------------------------


def title_for(video: dict[str, Any], site_name: str | None, collection_id: str) -> str:
    label = site_name or collection_id
    stem = _stem(video.get("file"), fallback=video.get("yt_id", ""))
    duration = video.get("duration_s")
    if isinstance(duration, (int, float)) and duration > 0:
        return f"{label} — {stem} ({round(duration)} s)"
    return f"{label} — {stem}"


def caption_for(
    video: dict[str, Any],
    site_name: str | None,
    caption_prefix: str | None = None,
) -> str:
    text = clean_caption(video.get("transcription"))
    if text:
        return f"{caption_prefix} — {text}" if caption_prefix else text
    # Empty / hallucinated transcript -> boilerplate fallback.
    if caption_prefix:
        return caption_prefix
    date = _date_only(video.get("captured_at"))
    label = site_name or "Site visit"
    return f"{label} — {date}" if date else label


def build_gallery(
    collection_id: str,
    videos: Iterable[dict[str, Any]],
    *,
    site_name: str | None = None,
    caption_prefix: str | None = None,
    generated: str | None = None,
) -> dict[str, Any]:
    """Assemble the gallery dict.  Deterministic for a given input set."""
    ordered = sorted(
        (v for v in videos if isinstance(v, dict) and v.get("yt_id")),
        key=lambda v: (str(v.get("captured_at") or ""), str(v.get("file") or "")),
    )
    entries: list[dict[str, Any]] = []
    for v in ordered:
        entries.append(
            {
                "type": "youtube",
                "videoId": v["yt_id"],
                "title": title_for(v, site_name, collection_id),
                "caption": caption_for(v, site_name, caption_prefix),
                "aspect": v.get("aspect") or "landscape",
            }
        )
    gallery: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "collection_id": collection_id,
    }
    if generated:
        gallery["generated"] = generated
    gallery["gallery"] = entries
    return gallery


def dumps(gallery: dict[str, Any]) -> str:
    """Canonical serialisation: 2-space indent, UTF-8 kept literal, newline."""
    return json.dumps(gallery, indent=2, ensure_ascii=False) + "\n"


def write_gallery(gallery: dict[str, Any], out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(gallery), encoding="utf-8")
    return path


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build a MAP gallery JSON.")
    parser.add_argument(
        "--collection", required=True, help="collection id (farm_id/program id)"
    )
    parser.add_argument("--inbox", help="inbox dir of MAP sidecars")
    parser.add_argument("--manifest", help="manifest v2 JSON (fallback source)")
    parser.add_argument("--site-name", default=None)
    parser.add_argument("--caption-prefix", default=None)
    parser.add_argument("--generated", default=None, help="optional timestamp string")
    parser.add_argument(
        "--no-probe", action="store_true", help="skip ffprobe aspect lookup"
    )
    parser.add_argument("--out", default="-", help="output path, or - for stdout")
    args = parser.parse_args(argv)

    videos: list[dict[str, Any]] = []
    if args.inbox:
        videos = gather_videos(args.inbox, probe=not args.no_probe)
    if not videos and args.manifest:
        manifest = _load_json(args.manifest)
        videos = videos_from_manifest(
            manifest, probe_dir=None if args.no_probe else args.inbox
        )
    gallery = build_gallery(
        args.collection,
        videos,
        site_name=args.site_name,
        caption_prefix=args.caption_prefix,
        generated=args.generated,
    )
    text = dumps(gallery)
    if args.out == "-":
        print(text, end="")
    else:
        write_gallery(gallery, args.out)
        print(f"wrote {args.out} ({len(gallery['gallery'])} entries)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
