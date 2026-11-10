#!/usr/bin/env python3
"""Daily Payout Schedule (PS) — render and email the unpaid-tree schedule.

Named per agentic_ai_context/sops/PAYOUT_REPORT_SOP.md §0.1:
document class "Payout Schedule" (PS),
filename PS_<program-slug>_<YYYY-MM-DD>_<count>trees.pdf.

WHAT THIS DOES
  Reads the ledger sheet (source of truth for paid-vs-unpaid), computes the trees that
  are PIX-registered but have no BOOKED payout event, renders a Payout Schedule PDF with
  real link annotations, and emails it as an attachment.

WHAT IT DELIBERATELY DOES NOT DO
  It does not pay anything, submit payout events, or move money. It produces a schedule.
  Payment remains gated on an explicit governor go.

WHY "BOOKED" AND NOT "ANY EVENT"
  payout events may carry status LEDGER_NOT_BOOKED, meaning the ledger legs never booked.
  Counting those as paid would understate what is owed.

DUPLICATE HANDLING
  Two mechanisms, deliberately belt-and-braces:
    1. request_transaction_id dedup (keep earliest capture) — retained per governor
       instruction. NOTE (measured 2026-11-10): this catches 0 of the 5 known duplicate
       captures, because request_transaction_id signs the *submission transaction*, not
       the photo, so each double-submit gets a distinct value.
    2. A maintained drop-list (state file). This is what actually reconciles
       222 outstanding -> 217 payable, matching the governor-approved PS.
  The drop-list is therefore load-bearing and must be maintained by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/truesight_autopilot")

import gspread  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    Paragraph,
    SimpleDocTemplate,
    Table,
    TableStyle,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daily_payout_schedule")

# --------------------------------------------------------------------- config

LEDGER_SHEET_ID = "17KwmxYOpTVR89ybRlOkDXoN9PF3UcaNu3REg2wNa83w"
TAB_TREES = "tree planting"
TAB_REGISTRATIONS = "payout registrations"
TAB_EVENTS = "payout events"

SA_NAME = "sunmint_ledger_manager"
PROGRAM_SLUG = "crf-anapu"
UNIT_BRL = 5.0

DEFAULT_TO = "garyjob@agroverse.shop"
DEFAULT_STATE = "/opt/truesight_autopilot/var/daily_payout_schedule_state.json"
DEFAULT_OUT_DIR = "/opt/truesight_autopilot/var/payout_schedules"

LINK_TREE = "https://cfr.truesight.me/my-trees/?tree="
LINK_EXPLORER = "https://beta.truesight.me/ledger/explorer/?event="

DRAFT_BANNER = "AUTO-GENERATED DRAFT — UNRECONCILED. Not a payout authorization."

TREE_ID_RE = re.compile(r"Edgar_\d+_\d+")

# --------------------------------------------------------------------- loading


def load_tab(sh, name: str) -> tuple[list[str], list[list[str]]]:
    ws = sh.worksheet(name)
    values = ws.get_all_values()
    if not values:
        raise SystemExit(f"tab {name!r} is empty")
    header = [str(h).strip() for h in values[0]]
    return header, values[1:]


def col(header: list[str], name: str, tab: str) -> int:
    try:
        return header.index(name)
    except ValueError:
        raise SystemExit(f"column {name!r} missing from tab {tab!r}") from None


def cell(row: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    v = row[idx]
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------------- compute


def compute_schedule(drop_ids: set[str]) -> dict:
    creds = None
    from app.tools.google_creds import load_credentials

    creds = load_credentials(
        SA_NAME, ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    if creds is None:
        raise SystemExit(f"no Google credentials for service account {SA_NAME!r}")
    sh = gspread.authorize(creds).open_by_key(LEDGER_SHEET_ID)

    t_h, t_rows = load_tab(sh, TAB_TREES)
    r_h, r_rows = load_tab(sh, TAB_REGISTRATIONS)
    e_h, e_rows = load_tab(sh, TAB_EVENTS)

    t_created = col(t_h, "created_at_utc", TAB_TREES)
    t_pk = col(t_h, "pk_hash", TAB_TREES)
    t_tree = col(t_h, "tree_id", TAB_TREES)
    t_species = col(t_h, "species", TAB_TREES)
    t_lat = col(t_h, "lat", TAB_TREES)
    t_lng = col(t_h, "lng", TAB_TREES)
    t_req = col(t_h, "request_transaction_id", TAB_TREES)

    r_pk = col(r_h, "pk_hash", TAB_REGISTRATIONS)
    r_status = col(r_h, "status", TAB_REGISTRATIONS)
    r_pix = col(r_h, "pix_key", TAB_REGISTRATIONS)

    e_tree = col(e_h, "tree_planting_id", TAB_EVENTS)
    e_status = col(e_h, "status", TAB_EVENTS)

    # ACTIVE PIX registrations only -> pk_hash : pix_key
    active_pix: dict[str, str] = {}
    for r in r_rows:
        if cell(r, r_status).upper() != "ACTIVE":
            continue
        pk = cell(r, r_pk)
        if pk:
            active_pix[pk] = cell(r, r_pix)

    # paid = tree ids with at least one BOOKED payout event
    paid: set[str] = set()
    booked_events = 0
    for r in e_rows:
        if cell(r, e_status).upper() != "BOOKED":
            continue
        booked_events += 1
        paid.update(TREE_ID_RE.findall(cell(r, e_tree)))

    payable: list[dict] = []
    dupes: list[dict] = []
    orphans: list[dict] = []
    seen_rti: dict[str, str] = {}

    ordered = sorted(t_rows, key=lambda r: cell(r, t_created))
    for r in ordered:
        tree_id = cell(r, t_tree)
        pk = cell(r, t_pk)
        if not tree_id or tree_id in paid:
            continue

        row = {
            "tree_id": tree_id,
            "species": cell(r, t_species),
            "created_at_utc": cell(r, t_created),
            "lat": cell(r, t_lat),
            "lng": cell(r, t_lng),
            "pk_hash": pk,
        }

        if pk not in active_pix:
            row["reason"] = "no ACTIVE PIX registration for pk_hash"
            orphans.append(row)
            continue
        row["pix_key"] = active_pix[pk]

        if tree_id in drop_ids:
            row["reason"] = "on maintained drop-list (duplicate capture)"
            dupes.append(row)
            continue

        rti = cell(r, t_req)
        if rti:
            if rti in seen_rti:
                row["reason"] = f"request_transaction_id reuse of {seen_rti[rti]}"
                dupes.append(row)
                continue
            seen_rti[rti] = tree_id

        is_pseudo = not row["lat"] or not row["lng"]
        row["is_pseudo"] = is_pseudo
        row["link"] = (LINK_EXPLORER if is_pseudo else LINK_TREE) + tree_id
        payable.append(row)

    pix_counts: dict[str, int] = {}
    for r in payable:
        pix_counts[r["pix_key"]] = pix_counts.get(r["pix_key"], 0) + 1

    return {
        "payable": payable,
        "dupes": dupes,
        "orphans": orphans,
        "paid_count": len(paid),
        "booked_events": booked_events,
        "total_brl": len(payable) * UNIT_BRL,
        "pix_counts": pix_counts,
        "tabs": {
            "tree_rows": len(t_rows),
            "registration_rows": len(r_rows),
            "event_rows": len(e_rows),
        },
    }


def signature_of(sched: dict) -> str:
    ids = sorted(r["tree_id"] for r in sched["payable"])
    orph = sorted(r["tree_id"] for r in sched["orphans"])
    h = hashlib.sha256()
    h.update("\n".join(ids).encode())
    h.update(b"|orphans|")
    h.update("\n".join(orph).encode())
    return f"{len(ids)}:{h.hexdigest()[:16]}"


# --------------------------------------------------------------------- render


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _money(n: float) -> str:
    return "R$ " + format(round(n, 2), ",.2f")


def render_pdf(sched: dict, date_str: str, out_path: Path) -> tuple[Path, int]:
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Title"], fontSize=15, spaceAfter=4, alignment=TA_LEFT
    )
    h2 = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4
    )
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11)
    small = ParagraphStyle("small", parent=body, fontSize=7, leading=8.5)
    warn = ParagraphStyle(
        "warn", parent=body, fontSize=8.5, textColor=colors.HexColor("#8a1c1c")
    )

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=out_path.stem,
    )
    story: list = []
    story.append(Paragraph(f"Payout Schedule — {_esc(PROGRAM_SLUG)}", h1))
    story.append(Paragraph(DRAFT_BANNER, warn))
    story.append(
        Paragraph(
            f"Generated {date_str} &nbsp;·&nbsp; {len(sched['payable'])} trees "
            f"&nbsp;·&nbsp; {_money(sched['total_brl'])} &nbsp;·&nbsp; "
            f"<b>STATUS: NOT PAID</b>",
            body,
        )
    )

    # 1. summary
    story.append(Paragraph("1. Summary", h2))
    summary_rows = [
        ["Trees payable", str(len(sched["payable"]))],
        ["Unit amount", f"{_money(UNIT_BRL)} per tree"],
        ["Total to transfer", _money(sched["total_brl"])],
        ["Duplicate captures excluded", str(len(sched["dupes"]))],
        ["Orphan trees (no PIX key)", str(len(sched["orphans"]))],
        [
            "Already paid (BOOKED events)",
            f"{sched['paid_count']} trees / {sched['booked_events']} events",
        ],
    ]
    t = Table(
        [[Paragraph(_esc(a), body), Paragraph(_esc(b), body)] for a, b in summary_rows],
        colWidths=[2.6 * inch, 4.4 * inch],
    )
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c9c9")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)

    # 2. PIX block
    story.append(Paragraph("2. Pay-to PIX account(s)", h2))
    if sched["pix_counts"]:
        pix_rows = [["PIX key", "Trees", "Amount"]]
        for k in sorted(sched["pix_counts"]):
            n = sched["pix_counts"][k]
            pix_rows.append([k, str(n), _money(n * UNIT_BRL)])
        pt = Table(
            pix_rows, colWidths=[3.0 * inch, 0.9 * inch, 1.3 * inch], repeatRows=1
        )
        pt.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9c9c9")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(pt)
    else:
        story.append(Paragraph("None.", body))

    # 3. per-item schedule with real link annotations
    story.append(Paragraph("3. Per-item schedule", h2))
    expected_links = 0
    if sched["payable"]:
        rows = [["#", "Tree ID", "Species", "Amount", "Coords / type", "Link"]]
        for i, r in enumerate(sched["payable"], 1):
            url = r["link"]
            link_cell = Paragraph(
                f'<link href="{_esc(url)}" color="#0b4f9c">view tree</link>', small
            )
            expected_links += 1
            rows.append(
                [
                    str(i),
                    Paragraph(_esc(r["tree_id"]), small),
                    Paragraph(_esc(r["species"] or "n/a"), small),
                    _money(UNIT_BRL),
                    Paragraph(
                        "pseudo tree"
                        if r["is_pseudo"]
                        else _esc(f"{r['lat']},{r['lng']}"),
                        small,
                    ),
                    link_cell,
                ]
            )
        it = Table(
            rows,
            colWidths=[
                0.35 * inch,
                1.62 * inch,
                1.32 * inch,
                0.68 * inch,
                1.35 * inch,
                0.72 * inch,
            ],
            repeatRows=1,
        )
        it.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d5d5d5")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#fafafa")],
                    ),
                ]
            )
        )
        story.append(it)
    else:
        story.append(Paragraph("No payable trees.", body))

    # 4. excluded
    story.append(Paragraph("4. Excluded", h2))
    if not sched["dupes"] and not sched["orphans"]:
        story.append(Paragraph("None.", body))
    if sched["dupes"]:
        story.append(Paragraph("<b>Duplicate captures (will not be paid)</b>", body))
        for r in sched["dupes"]:
            story.append(
                Paragraph(f"• {_esc(r['tree_id'])} — {_esc(r['reason'])}", small)
            )
    if sched["orphans"]:
        story.append(
            Paragraph(
                "<b>Orphan trees — no ACTIVE PIX registration, unpayable until a key exists</b>",
                body,
            )
        )
        for r in sched["orphans"]:
            story.append(
                Paragraph(
                    f"• {_esc(r['tree_id'])} — pk_hash {_esc(r['pk_hash'])} — "
                    f"{_esc(r['species'] or 'n/a')} — {_esc(r['created_at_utc'])}",
                    small,
                )
            )

    # 5. definitions / footer
    story.append(Paragraph("5. Definitions", h2))
    for line in [
        "Tree ID = canonical tree_planting_id from the ledger (sheet column D).",
        "Pseudo tree = row with blank lat/lng; provenance resolves via the Ledger Explorer fallback.",
        "Paid = tree carries at least one BOOKED payout event. LEDGER_NOT_BOOKED is not paid.",
        "Excluded duplicates are identified by photo content (Timemark code + file MD5), "
        "which are not ledger columns; the maintained drop-list is the authoritative source.",
        "STATUS: NOT PAID. This schedule authorizes nothing; payment requires an explicit governor go.",
    ]:
        story.append(Paragraph(line, small))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)
    return out_path, expected_links


def count_pdf_links(path: Path) -> int:
    import fitz

    total = 0
    with fitz.open(str(path)) as d:
        for page in d:
            total += sum(1 for ln in page.get_links() if ln.get("uri"))
    return total


# --------------------------------------------------------------------- state


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------- main


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Render and email the daily Payout Schedule (PS)."
    )
    p.add_argument("--to", default=DEFAULT_TO)
    p.add_argument(
        "--account", default="admin", help="gmail token account to send from"
    )
    p.add_argument("--state-file", default=DEFAULT_STATE)
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--dry-run", action="store_true", help="compute + report only")
    p.add_argument(
        "--skip-email", action="store_true", help="render PDF but do not send"
    )
    p.add_argument(
        "--force", action="store_true", help="send even if unpaid set unchanged"
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    state_path = Path(args.state_file)
    state = load_state(state_path)
    drop_ids = set(state.get("drop_tree_ids", []))

    sched = compute_schedule(drop_ids)
    sig = signature_of(sched)
    last_sig = state.get("last_signature", "")

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    report = {
        "date": date_str,
        "payable": len(sched["payable"]),
        "total_brl": sched["total_brl"],
        "dupes_excluded": len(sched["dupes"]),
        "dupes": [
            {"tree_id": d["tree_id"], "reason": d["reason"]} for d in sched["dupes"]
        ],
        "orphans": len(sched["orphans"]),
        "orphan_ids": [o["tree_id"] for o in sched["orphans"]],
        "already_paid_trees": sched["paid_count"],
        "booked_events": sched["booked_events"],
        "pix": sched["pix_counts"],
        "signature": sig,
        "signature_changed": sig != last_sig,
        "tabs": sched["tabs"],
    }

    if args.dry_run:
        report["dry_run"] = True
        report["sent"] = False
        print(json.dumps(report, indent=2) if args.json else _human(report))
        return 0

    if not sched["payable"]:
        report["sent"] = False
        report["reason"] = "no unpaid PIX-registered trees"
        print(json.dumps(report, indent=2) if args.json else _human(report))
        return 0

    if not args.force and sig == last_sig:
        report["sent"] = False
        report["reason"] = (
            "unpaid set unchanged since last send (use --force to override)"
        )
        print(json.dumps(report, indent=2) if args.json else _human(report))
        return 0

    fname = f"PS_{PROGRAM_SLUG}_{date_str}_{len(sched['payable'])}trees.pdf"
    out_path = Path(args.out_dir) / fname
    pdf_path, expected = render_pdf(sched, date_str, out_path)
    got = count_pdf_links(pdf_path)

    report.update(
        {
            "file": str(pdf_path),
            "file_bytes": pdf_path.stat().st_size,
            "expected_links": expected,
            "pdf_links": got,
            "links_verified": expected > 0 and got == expected,
        }
    )
    if expected != got:
        log.warning("link annotation mismatch: expected %d got %d", expected, got)

    subject = f"Payout Schedule: {len(sched['payable'])} trees, {_money(sched['total_brl'])} — NOT PAID ({date_str})"
    body = "\n".join(
        [
            f"Daily Payout Schedule for {PROGRAM_SLUG} — {date_str}",
            "",
            f"Trees payable:            {len(sched['payable'])}",
            f"Total:                    {_money(sched['total_brl'])} at {_money(UNIT_BRL)}/tree",
            f"Duplicate captures excl.: {len(sched['dupes'])}",
            f"Orphan trees (no PIX key):{len(sched['orphans'])}",
            f"Already paid (BOOKED):    {sched['paid_count']} trees / {sched['booked_events']} events",
            "",
            "STATUS: NOT PAID. The attached schedule authorizes nothing; payment",
            "requires an explicit governor go.",
            "",
            f"Link annotations verified in PDF: {got} / {expected}",
        ]
    )
    report["subject"] = subject

    if args.skip_email:
        report["sent"] = False
        report["reason"] = "skip-email requested"
        print(json.dumps(report, indent=2) if args.json else _human(report))
        return 0

    from app.tools.gmail_tools import gmail_send

    res = gmail_send(
        to=args.to,
        subject=subject,
        body=body,
        account=args.account,
        attachment_path=str(pdf_path),
    )
    ok = isinstance(res, str) and ("ok" in res.lower() or "sent" in res.lower())
    report["send_result"] = str(res)[:300]
    report["sent"] = bool(ok)

    if ok:
        state["last_signature"] = sig
        state["last_sent_utc"] = now.isoformat()
        state["last_count"] = len(sched["payable"])
        state["last_total_brl"] = sched["total_brl"]
        state.setdefault("drop_tree_ids", sorted(drop_ids))
        save_state(state_path, state)

    print(json.dumps(report, indent=2) if args.json else _human(report))
    return 0 if ok else 1


def _human(r: dict) -> str:
    lines = [
        f"Payout Schedule {r['date']}",
        f"  payable            : {r['payable']} trees  ({_money(r['total_brl'])})",
        f"  dupes excluded     : {r['dupes_excluded']}",
        f"  orphans            : {r['orphans']}  {r['orphan_ids']}",
        f"  already paid       : {r['already_paid_trees']} trees / {r['booked_events']} BOOKED events",
        f"  pix                : {r['pix']}",
        f"  signature          : {r['signature']} (changed={r['signature_changed']})",
        f"  sent               : {r.get('sent')}",
    ]
    if r.get("reason"):
        lines.append(f"  reason             : {r['reason']}")
    if "pdf_links" in r:
        lines.append(
            f"  links              : {r['pdf_links']}/{r['expected_links']} verified={r['links_verified']}"
        )
    if r.get("file"):
        lines.append(f"  file               : {r['file']} ({r['file_bytes']} bytes)")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
