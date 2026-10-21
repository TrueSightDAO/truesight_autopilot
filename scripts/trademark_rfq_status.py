#!/usr/bin/env python3
"""Daily trademark-RFQ status digest + guarded chase emails.

Runs from cron once a day. For the BIONPACT Singapore trade mark RFQ
(Sophia, 2026-10-16) it:

  1. Reads Gmail for inbound mail from each instructed firm.
  2. Classifies each firm: QUOTED / BLOCKED_ON_US / ACKNOWLEDGED / AWAITING.
  3. Renders a status table, posts it to Discord, and attaches a PDF.
  4. Sends a chase email ONLY when the cadence guards allow it:
       - firm is AWAITING or ACKNOWLEDGED (never QUOTED, never BLOCKED_ON_US)
       - >= CHASE_GAP_DAYS since the last inbound and since our last chase
       - fewer than MAX_CHASES already sent
       - not a weekend
     Firms blocked on US are surfaced as an action item, never auto-chased.

Never auto-chases a firm that has quoted, and never exceeds MAX_CHASES --
after that it escalates in the report for a human decision.

Usage:
    python3 trademark_rfq_status.py              # report only, no email
    python3 trademark_rfq_status.py --send       # report + chases if due
    python3 trademark_rfq_status.py --dry-run    # print everything, send nothing
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path("/opt/truesight_autopilot")
STATE_PATH = Path("/home/ubuntu/scripts/trademark_rfq_state.json")
REPORT_DIR = Path("/tmp/trademark_rfq")
TOKEN_PATH = APP_DIR / "config/gmail/admin_token.json"

DISCORD_CHANNEL_ID = "1560756241070952478"
FROM_ADDR = "Sophia Truesight <admin+sophia@truesight.me>"
CC_ADDR = "garyjob@gmail.com"

MAX_CHASES = 2
CHASE_GAP_DAYS = 3

# ── the instructed firms ──────────────────────────────────────────────────
FIRMS = [
    {
        "key": "marksclerk",
        "name": "Marks & Clerk Singapore LLP",
        "domain": "marks-clerk.com.sg",
        "to": "twkwok@marks-clerk.com.sg",
        "contact": "Tat Wai Kwok",
        "sent": "2026-10-16",
    },
    {
        "key": "amica",
        "name": "Amica Law LLC",
        "domain": "amicalaw.com",
        "to": "winnie.tham@amicalaw.com",
        "contact": "Winnie Tham",
        "sent": "2026-10-16",
    },
    {
        "key": "ellacheong",
        "name": "Ella Cheong LLC",
        "domain": "ellacheong.asia",
        "to": "brendan.lon@ellacheong.asia",
        "contact": "Brendan Lon",
        "sent": "2026-10-16",
    },
    {
        "key": "hjm",
        "name": "HJM Asia Law & Co LLC",
        "domain": "hjmasialaw.com",
        "to": "cberube@hjmasialaw.com",
        "contact": "Caroline Berube",
        "sent": "2026-10-16",
    },
    {
        "key": "donaldson",
        "name": "Donaldson & Burkinshaw LLP",
        "domain": "donburk.asia",
        "to": "salsabil@donburk.asia",
        "contact": "Salsabil Bte Sata / Min Pin Koay",
        "sent": "2026-10-16",
    },
    {
        "key": "spruson",
        "name": "Spruson & Ferguson",
        "domain": "spruson.com",
        "to": "mail.asia@spruson.com",
        "contact": "mail.asia (Amy Chan)",
        "sent": "2026-10-16",
    },
]

# Curated fee summary, kept current as quotations land. The daily report is a
# STATUS digest -- prose parsing of 6 firms' quotations is not reliable enough
# to auto-extract money figures, so the verified numbers live here.
QUOTES = {
    "marksclerk": {
        "prof": "SGD 950",
        "prof_note": "per application, less 10% offered",
        "official": "SGD 280-410",
        "extra_class": "n/a in quote",
        "search": "SGD 400/mark fixed",
        "timeline": "not stated",
    },
    "amica": {
        "prof": "SGD 750",
        "prof_note": "500 filing + 250 registration, per class",
        "official": "SGD 280-410",
        "extra_class": "+SGD 550 prof + official",
        "search": "SGD 250 knockout; from SGD 600 full",
        "timeline": "9-12 mths (3-5 accelerated)",
    },
    "ellacheong": {
        "prof": "SGD 1,440",
        "prof_note": "incl. drafting spec + reporting, per class",
        "official": "SGD 280",
        "extra_class": "+SGD 990 prof + SGD 280",
        "search": "SGD 510 + SGD 20 disb",
        "timeline": "up to 9 mths exam + 2 mth opposition",
    },
    "hjm": {
        "prof": "USD 895",
        "prof_note": "per TM per class; +USD 200 file opening",
        "official": "SGD 280-410",
        "extra_class": "USD 700/class",
        "search": "USD 350 availability check",
        "timeline": "~9 months",
    },
}

REPORT_BLOCKED = ("BLOCKED_ON_US", "ACKNOWLEDGED", "AWAITING")


# ── gmail ─────────────────────────────────────────────────────────────────
def gmail_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    raw = json.loads(TOKEN_PATH.read_text())
    creds = Credentials(
        token=raw.get("token"),
        refresh_token=raw.get("refresh_token"),
        token_uri=raw.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=raw.get("client_id"),
        client_secret=raw.get("client_secret"),
        scopes=raw.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        try:
            TOKEN_PATH.write_text(creds.to_json())
        except Exception:
            pass
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _body(payload) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
            "utf-8", "replace"
        )
    out = ""
    for part in payload.get("parts", []) or []:
        out += _body(part)
    return out


def _plain(raw: str) -> str:
    txt = re.sub(r"<(script|style).*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    return re.sub(r"[ \t]{2,}", " ", txt)


def classify(text: str) -> str:
    """Classify one inbound firm email.

    Fee tables often arrive as HTML whose cells flatten to bare numbers
    ("Filing a Singapore trademark application ... | 1,440 | 280 | 40"), so a
    money-adjacent-to-fee-words test alone is not enough -- also treat a
    document that mentions fees AND has several 3+ digit figures as a quote.
    """
    low = text.lower()
    money_hits = re.findall(
        r"(?:(?:sgd|s\$|usd|us\$)\s?[\d,]{3,})|(?:(?<![\d.,])\d{1,3}(?:,\d{3})+(?!\d))",
        low,
    )
    quote_words = (
        "professional fee",
        "official fee",
        "quotation",
        "our fees",
        "service fees",
        "fees in sgd",
        "estimated fees",
        "fee quote",
        "filing a singapore trademark application",
    )
    has_quote_word = any(w in low for w in quote_words)
    if money_hits and (has_quote_word or len(money_hits) >= 3):
        return "QUOTED"
    blocked = (
        "conflict check",
        "applicant's details",
        "applicant details",
        "full name and address of the applicant",
        "name and address",
    )
    if any(b in low for b in blocked):
        return "BLOCKED_ON_US"
    ack = ("revert", "receiving attention", "shortly", "looking into", "will respond")
    if any(a in low for a in ack):
        return "ACKNOWLEDGED"
    return "RESPONDED"


def scan_firm(svc, firm) -> dict:
    """Return latest inbound mail + classification for one firm."""
    q = f"from:{firm['domain']} newer_than:30d"
    res = svc.users().messages().list(userId="me", q=q, maxResults=25).execute()
    msgs = []
    for ref in res.get("messages", []) or []:
        m = (
            svc.users()
            .messages()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
        hdrs = {h["name"].lower(): h["value"] for h in m["payload"]["headers"]}
        msgs.append(
            {
                "id": ref["id"],
                "date": hdrs.get("date", ""),
                "from": hdrs.get("from", ""),
                "subject": hdrs.get("subject", ""),
                "text": _plain(_body(m["payload"])),
                "internal_ts": int(m.get("internalDate", "0")) / 1000.0,
            }
        )
    if not msgs:
        return {"status": "AWAITING", "count": 0, "last": None, "last_ts": None}
    msgs.sort(key=lambda x: x["internal_ts"], reverse=True)
    classes = [classify(m["text"]) for m in msgs]
    # QUOTED wins if it ever arrived; else the most specific non-quote state.
    if "QUOTED" in classes:
        status = "QUOTED"
    elif "BLOCKED_ON_US" in classes:
        status = "BLOCKED_ON_US"
    elif "ACKNOWLEDGED" in classes:
        status = "ACKNOWLEDGED"
    else:
        status = "RESPONDED"
    return {
        "status": status,
        "count": len(msgs),
        "last": msgs[0],
        "last_ts": msgs[0]["internal_ts"],
        "last_from": msgs[0]["from"],
        "last_date": msgs[0]["date"],
    }


# ── state ─────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"firms": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── chase decision ────────────────────────────────────────────────────────
def chase_due(firm, info, state, now) -> tuple[bool, str]:
    st = state["firms"].setdefault(firm["key"], {"chases": 0, "last_chase": None})
    status = info["status"]
    if status == "QUOTED":
        return False, "already quoted"
    if status == "BLOCKED_ON_US":
        return False, "blocked on us (applicant details)"
    if now.weekday() >= 5:
        return False, "weekend"
    if st["chases"] >= MAX_CHASES:
        return False, f"chase cap reached ({st['chases']})"
    baseline = info["last_ts"]
    if baseline:
        idle = (now.timestamp() - baseline) / 86400.0
    else:
        sent = datetime.fromisoformat(firm["sent"]).replace(tzinfo=timezone.utc)
        idle = (now - sent).days
    if st["last_chase"]:
        since_chase = (now - datetime.fromisoformat(st["last_chase"])).days
        if since_chase < CHASE_GAP_DAYS:
            return False, f"chased {since_chase}d ago (<{CHASE_GAP_DAYS}d)"
    if idle < CHASE_GAP_DAYS:
        return False, f"only {idle:.1f}d idle"
    return True, f"idle {idle:.1f}d, chases={st['chases']}"


def chase_body(firm) -> str:
    return f"""Dear {firm["contact"]},

I am following up on our request for a quotation, sent on 16 October 2026, for
registering the trade mark BIONPACT in Singapore.

We have not yet received a quotation from your firm and would be grateful for
your indication of fees so that we can compare proposals and proceed.

For ease of reference, we asked for:
  1. The class(es) you would recommend;
  2. Professional fees for a single-class filing, plus the per-class fee for
     any additional class;
  3. IPOS official filing fees and disbursements itemised separately;
  4. The timeline from instruction to registration;
  5. Whether an initial registrability / clearance search is included, or
     billed separately and at what cost.

The mark is the word mark "BIONPACT" together with a figurative/logo device
(the image was attached to our original email). We are considering a single
class at this stage.

Kindly reply to admin+sophia@truesight.me, copying Gary Teh at
garyjob@gmail.com. As I am an AI assistant writing on behalf of the client,
please address your reply to the above addresses.

Thank you.
Best regards,
Sophia Truesight
admin+sophia@truesight.me
TrueSight DAO
"""


def send_mail(svc, to, subject, body) -> str:
    from email.mime.text import MIMEText

    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Cc"] = CC_ADDR
    msg["From"] = FROM_ADDR
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    res = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return res.get("id", "")


# ── report ────────────────────────────────────────────────────────────────
def build_report(results, state, now) -> tuple[str, str]:
    """Return (markdown, title)."""
    quoted = [f for f in FIRMS if results[f["key"]]["status"] == "QUOTED"]
    blocked = [f for f in FIRMS if results[f["key"]]["status"] == "BLOCKED_ON_US"]
    waiting = [f for f in FIRMS if f["key"] not in {x["key"] for x in quoted + blocked}]

    d = now.strftime("%A %d %B %Y")
    lines = [
        f"# BIONPACT — Singapore Trade Mark RFQ · Status {d}",
        "",
        f"**{len(quoted)}/{len(FIRMS)} firms have quoted** · "
        f"{len(blocked)} blocked on us · {len(waiting)} outstanding",
        "",
        "| Firm | Status | Professional fee | IPOS official | Search | Timeline |",
        "|---|---|---|---|---|---|",
    ]
    icon = {
        "QUOTED": "✅ Quoted",
        "BLOCKED_ON_US": "⛔ Needs our input",
        "ACKNOWLEDGED": "⏳ Acknowledged",
        "AWAITING": "🔇 No reply",
        "RESPONDED": "💬 Responded",
    }
    for f in FIRMS:
        info = results[f["key"]]
        q = QUOTES.get(f["key"], {})
        lines.append(
            "| **{}** | {} | {} | {} | {} | {} |".format(
                f["name"],
                icon.get(info["status"], info["status"]),
                q.get("prof", "—"),
                q.get("official", "—"),
                q.get("search", "—"),
                q.get("timeline", "—"),
            )
        )

    lines += ["", "## Detail", ""]
    for f in FIRMS:
        info = results[f["key"]]
        q = QUOTES.get(f["key"], {})
        st = state["firms"].get(f["key"], {})
        note = []
        if info.get("last_date"):
            note.append(f"last inbound {info['last_date'][:22]} ({info['count']} msg)")
        else:
            note.append("no inbound mail")
        if q.get("prof_note"):
            note.append(q["prof_note"])
        if q.get("extra_class") and q.get("extra_class") != "n/a in quote":
            note.append(f"extra class {q['extra_class']}")
        if st.get("chases"):
            note.append(f"chases sent {st['chases']}")
        lines.append(f"- **{f['name']}** — {'; '.join(note)}")

    if blocked:
        lines += ["", "## ⛔ Blocked on us — action required", ""]
        for f in blocked:
            lines.append(
                f"- **{f['name']}** wants the **applicant's full name and address** "
                "for their internal conflict check before quoting. As a DAO may not "
                "have separate legal personality, the application must be filed by an "
                "individual or a legal entity that will own the mark."
            )
    if quoted:
        lines += ["", "## ✅ Quotations received", ""]
        for f in quoted:
            lines.append(
                f"- {f['name']} — {QUOTES.get(f['key'], {}).get('prof', 'see quote')}"
            )

    lines += [
        "",
        "## Open decision needed from Gary",
        "",
        "1. **Applicant legal entity** — which entity/individual owns the mark "
        "(e.g. BIONPACT PTE. LTD.?) and its registered address.",
        "2. **Goods/services** — a short description so firms can confirm the class "
        "(Class 5 if a nutraceutical/health product, Class 42 if software, etc.).",
        "3. **One mark or two** — Amica notes the word mark and logo are separate "
        "applications; the word mark alone protects BIONPACT in any presentation.",
        "",
        f"*Generated automatically {now.strftime('%Y-%m-%d %H:%M UTC')} by "
        "`trademark_rfq_status.py`. Chase cadence: max "
        f"{MAX_CHASES} per firm, >= {CHASE_GAP_DAYS} days apart, weekdays only.*",
    ]
    return "\n".join(
        lines
    ), f"BIONPACT Trademark RFQ — Status {now.strftime('%Y-%m-%d')}"


def render_pdf(markdown: str, title: str, out: Path) -> bool:
    """Render the report to PDF. ``generate_pdf`` returns a JSON *string*."""
    sys.path.insert(0, str(APP_DIR))
    try:
        from app.tools.pdf_tools import generate_pdf as _gen

        res = _gen(content=markdown, title=title, output_path=str(out))
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                res = {}
        if isinstance(res, dict) and res.get("status") not in (None, "ok"):
            print(f"PDF render error: {res.get('error')}", file=sys.stderr)
            return False
        return out.exists() and out.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        print(f"PDF render failed: {e}", file=sys.stderr)
        return False


def post_discord(text: str, pdf: Path | None) -> None:
    sys.path.insert(0, str(APP_DIR))
    from app.vault import get_vault

    token = get_vault().get_value("DISCORD_BOT_TOKEN")
    # chunk for Discord's 2000-char limit
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    for c in chunks:
        subprocess.run(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                "-H",
                f"Authorization: Bot {token}",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps({"content": c}),
            ],
            check=False,
            capture_output=True,
        )
    if pdf and pdf.exists():
        subprocess.run(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
                "-H",
                f"Authorization: Bot {token}",
                "-F",
                f"payload_json={json.dumps({'content': 'Status report (PDF)'})}",
                "-F",
                f"files[0]=@{pdf}",
            ],
            check=False,
            capture_output=True,
        )


# ── main ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="send chase emails where due")
    ap.add_argument("--dry-run", action="store_true", help="print only, no post/email")
    ap.add_argument("--no-discord", action="store_true", help="skip Discord posting")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    svc = gmail_service()
    state = load_state()

    results = {}
    for f in FIRMS:
        try:
            results[f["key"]] = scan_firm(svc, f)
        except Exception as e:  # noqa: BLE001
            print(f"scan failed for {f['key']}: {e}", file=sys.stderr)
            results[f["key"]] = {
                "status": "AWAITING",
                "count": 0,
                "last": None,
                "last_ts": None,
            }

    actions = []
    for f in FIRMS:
        due, why = chase_due(f, results[f["key"]], state, now)
        if due:
            st = state["firms"].setdefault(f["key"], {"chases": 0, "last_chase": None})
            if args.send and not args.dry_run:
                try:
                    mid = send_mail(
                        svc,
                        f["to"],
                        "Follow-up: Request for Quotation \u2014 Trade Mark Registration "
                        "in Singapore (mark: BIONPACT)",
                        chase_body(f),
                    )
                    st["chases"] = st.get("chases", 0) + 1
                    st["last_chase"] = now.isoformat()
                    actions.append(f"chased {f['name']} -> {mid}")
                except Exception as e:  # noqa: BLE001
                    actions.append(f"chase FAILED {f['name']}: {e}")
            else:
                actions.append(f"[dry] would chase {f['name']} ({why})")
        else:
            actions.append(f"skip {f['name']}: {why}")

    state["last_run"] = now.isoformat()
    if not args.dry_run:
        save_state(state)

    markdown, title = build_report(results, state, now)
    if actions:
        markdown += "\n\n## Chase actions this run\n\n" + "\n".join(
            f"- {a}" for a in actions
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = REPORT_DIR / f"bionpact_rfq_status_{now.strftime('%Y%m%d')}.pdf"
    has_pdf = render_pdf(markdown, title, pdf_path)

    print(markdown)
    print(f"\n[pdf] {pdf_path if has_pdf else 'FAILED'}")

    if not args.dry_run and not args.no_discord:
        post_discord(markdown, pdf_path if has_pdf else None)
        print("[discord] posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
