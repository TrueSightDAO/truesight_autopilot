#!/usr/bin/env python3
"""Repair Content-Type metadata on media-archive objects (S3).

Background
----------
Objects uploaded to ``media.agroverse.shop`` under ``raw/`` arrive with
``Content-Type: binary/octet-stream`` because the uploader never set one.
Browsers then refuse to play ``.MOV`` files inline and treat ``.HEIC`` as a
download. ``previews/`` is already correct (``image/jpeg`` + immutable cache).

Naming note: the original roadmap called this ``fix_poster_content_type.py``
and targeted a ``posters/`` prefix. No ``posters/`` prefix exists in the
bucket or in ``MEDIA_ARCHIVE_PIPELINE.md`` -- the pipeline produces *frame
previews* at ``previews/<farm>/<basename>.jpg``. So this script defaults to
``raw/``, which is where the real defect is.

Usage
-----
    python scripts/fix_media_content_type.py --dry-run
    python scripts/fix_media_content_type.py --apply

Safe by design: ``--dry-run`` is the default. ``--apply`` rewrites metadata
only (CopyObject to itself with ``MetadataDirective=REPLACE``), preserving
StorageClass and Cache-Control. Re-running is a no-op once fixed.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys

BUCKET = "media.agroverse.shop"
ACCOUNT = "nelanco"
DEFAULT_PREFIX = "raw/"

# Extension -> Content-Type. Deliberately explicit rather than mimetypes,
# which returns application/octet-stream for .heic on older Pythons.
CONTENT_TYPES = {
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".dng": "image/x-adobe-dng",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
}

# Values we treat as "unset".
BAD_TYPES = {"", "binary/octet-stream", "application/octet-stream", "None"}


def load_s3():
    """Build an S3 client for the media account, reading creds from .env.

    systemd loads /opt/truesight_autopilot/.env via EnvironmentFile, but an
    interactive SSH shell does not -- so source it here if needed.
    """
    try:
        from dotenv import load_dotenv

        for candidate in (
            os.path.join(os.path.dirname(__file__), os.pardir, ".env"),
            "/opt/truesight_autopilot/.env",
        ):
            candidate = os.path.abspath(candidate)
            if os.path.exists(candidate):
                load_dotenv(candidate, override=False)
                break
    except ImportError:
        pass

    # Find the autopilot repo root so `app` is importable, wherever this runs.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.abspath(os.path.join(here, os.pardir)),  # scripts/ -> repo root
        os.getcwd(),
        "/opt/truesight_autopilot",
        os.environ.get("AUTOPILOT_ROOT", ""),
    ]
    for root in candidates:
        if root and os.path.isdir(os.path.join(root, "app")):
            sys.path.insert(0, root)
            break
    else:
        raise SystemExit("cannot locate the autopilot repo root (no app/ dir found)")

    from app.aws_monitor import read_account_specs

    import boto3

    spec = next(
        (s for s in read_account_specs() if s["label"] == ACCOUNT.lower()), None
    )
    if spec is None:
        raise SystemExit(
            f"no '{ACCOUNT}' credentials found -- .env must set "
            f"AWS_ACCESS_KEY_ID_{ACCOUNT.upper()} / AWS_SECRET_ACCESS_KEY_{ACCOUNT.upper()}"
        )
    return boto3.Session(
        aws_access_key_id=spec["key_id"],
        aws_secret_access_key=spec["secret"],
        region_name=spec["region"],
    ).client("s3")


def list_keys(s3, bucket: str, prefix: str) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        out.extend(resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")


def ext_of(key: str) -> str:
    base = key.rsplit("/", 1)[-1]
    return ("." + base.rsplit(".", 1)[-1].lower()) if "." in base else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only (default)")
    ap.add_argument(
        "--apply", action="store_true", help="actually rewrite Content-Type"
    )
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument(
        "--limit", type=int, default=0, help="cap objects inspected (0 = all)"
    )
    args = ap.parse_args()

    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    apply_mode = args.apply

    s3 = load_s3()
    keys = list_keys(s3, args.bucket, args.prefix)
    if args.limit:
        keys = keys[: args.limit]

    print(f"bucket={args.bucket}  prefix={args.prefix!r}  objects={len(keys)}")
    print(f"mode={'APPLY' if apply_mode else 'DRY-RUN'}\n")

    stats = collections.Counter()
    bad_by_ext = collections.Counter()
    unknown_ext = collections.Counter()
    changed = 0

    for obj in keys:
        key = obj["Key"]
        ext = ext_of(key)
        want = CONTENT_TYPES.get(ext)
        head = s3.head_object(Bucket=args.bucket, Key=key)
        have = head.get("ContentType", "")

        if want is None:
            stats["unknown_extension"] += 1
            unknown_ext[ext] += 1
            continue
        if have == want:
            stats["already_correct"] += 1
            continue

        stats["needs_fix"] += 1
        bad_by_ext[ext] += 1
        if not apply_mode:
            continue

        copy_src = {"Bucket": args.bucket, "Key": key}
        extra = {
            "Bucket": args.bucket,
            "Key": key,
            "CopySource": copy_src,
            "MetadataDirective": "REPLACE",
            "ContentType": want,
        }
        # Preserve storage class / cache-control / metadata we found.
        if head.get("StorageClass") and head["StorageClass"] != "STANDARD":
            extra["StorageClass"] = head["StorageClass"]
        if head.get("CacheControl"):
            extra["CacheControl"] = head["CacheControl"]
        if head.get("Metadata"):
            extra["Metadata"] = head["Metadata"]
        s3.copy_object(**extra)
        changed += 1
        if changed % 100 == 0:
            print(f"  ... {changed} rewritten")

    print("--- summary ---")
    for k in ("already_correct", "needs_fix", "unknown_extension"):
        print(f"{k:18} {stats.get(k, 0)}")
    if bad_by_ext:
        print("\nwould-fix by extension:")
        for ext, n in bad_by_ext.most_common():
            print(f"  {ext or '(none)':8} {n:5}  -> {CONTENT_TYPES.get(ext)}")
    if unknown_ext:
        print("\nunknown extensions (skipped):")
        for ext, n in unknown_ext.most_common():
            print(f"  {ext or '(none)':8} {n:5}")

    if apply_mode:
        print(f"\nobjects rewritten: {changed}")
    else:
        print(f"\nDRY-RUN: 0 objects modified. Would fix: {stats.get('needs_fix', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
