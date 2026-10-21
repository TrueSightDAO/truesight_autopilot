"""Tests for the autonomous-correspondence policy.

Two governor decisions drive these (Gary, 2026-10-20):
  * don't spend time on obvious marketing / non-genuine mail
  * keep extracting information until the correspondent asks for a
    face-to-face, then hand the thread to a human
"""

from datetime import datetime, timezone

import pytest

from app.email_watch_policy import (
    format_meeting_escalation,
    is_bulk,
    is_bulk_by_headers,
    meeting_requested,
    per_thread_cap,
    thread_reply_allowed,
)

# Real correspondents from the 2026-10 trademark RFQ + business inbound.
# A false positive here means we go silent on a live commercial thread.
REAL_CORRESPONDENTS = [
    {"from": "Winnie Tham <winnie.tham@amicalaw.com.sg>", "subject": "Re: Trade mark"},
    {"from": "Brendan Lon <brendan.lon@ellacheong.asia>", "subject": "RE: RFQ"},
    {"from": "Caroline Berube <cberube@hjmasialaw.com>", "subject": "RE: filing"},
    {"from": "Tat Wai Kwok <tatwai.kwok@marks-clerk.com>", "subject": "Re: RFQ"},
    {
        "from": "Koay Min Pin <minpin.koay@donaldsons.com.sg>",
        "subject": "Re: trade mark",
    },
    {"from": "guilherme@exportbr.com.br", "subject": "Brazilian cacao - China"},
    {"from": "Joe Berman <joe@brandzconnect.com>", "subject": "Re: follow up"},
]

BULK_MAIL = [
    {"from": "TikTok Shop <noreply@shop.tiktok.com>", "subject": "Your verification"},
    {"from": "deals@wigsandbeauty.com", "subject": "50% off flash sale - last chance"},
    {"from": "newsletter@retaildive.com", "subject": "Retail Dive Daily"},
    {"from": "shop@vendor.com", "subject": "Limited time: free shipping, shop now"},
    {"from": "notifications@github.com", "subject": "[GitHub] A new sign-in"},
]


@pytest.mark.parametrize("row", REAL_CORRESPONDENTS)
def test_real_correspondents_are_never_bulk(row):
    assert is_bulk(row) is None, f"would have silenced {row['from']}"


@pytest.mark.parametrize("row", BULK_MAIL)
def test_bulk_mail_is_flagged(row):
    assert is_bulk(row) is not None


def test_sender_denylist_is_env_configurable(monkeypatch):
    monkeypatch.setenv("EMAIL_WATCH_SENDER_DENYLIST", "spam.example, evil.test")
    row = {"from": "someone@spam.example", "subject": "hello"}
    assert is_bulk(row) == "denylist"


@pytest.mark.parametrize(
    "headers",
    [
        {"List-Unsubscribe": "<https://example.com/u>"},
        {"List-Id": "<news.example.com>"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
    ],
)
def test_header_gates_flag_bulk(headers):
    assert is_bulk_by_headers(headers) is not None


def test_header_gates_ignore_normal_mail():
    assert is_bulk_by_headers({"Subject": "Re: quotation"}) is None
    # 'first-class' is a legitimate Precedence value for transactional mail.
    assert is_bulk_by_headers({"Precedence": "first-class"}) is None


@pytest.mark.parametrize(
    "text",
    [
        "Would you be free for a call next week?",
        "Would you be available to discuss the specification?",
        "Perhaps we could jump on a Zoom to discuss the specification.",
        "I'd like to arrange a meeting to discuss the applicant entity.",
        "Are you available 4pm SGT on 21 Oct?",
        "Happy to meet face to face if convenient.",
        "Let's get on a call.",
        "Do you have a quick call available?",
        "We could schedule a short call with your team.",
        "Shall we have a chat about the classes?",
    ],
)
def test_meeting_requests_are_detected(text):
    assert meeting_requested(text) is not None, f"missed escalation: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "Our fee for one class is SGD 1,160 plus disbursements.",
        "Please find our quotation attached.",
        "We confirm receipt of your payment of SGD 1,160.",
        "Registration takes 9 to 12 months from filing.",
        "The applicant entity must be named in the application.",
        "",
    ],
)
def test_non_meeting_mail_does_not_escalate(text):
    assert meeting_requested(text) is None, f"false escalation: {text}"


def test_thread_reply_cap_reserves_and_blocks(monkeypatch):
    monkeypatch.setenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", "2")
    state: dict = {}
    now = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
    assert thread_reply_allowed(state, "t1", now) is True
    assert thread_reply_allowed(state, "t1", now) is True
    # third automated reply in the same thread is refused
    assert thread_reply_allowed(state, "t1", now) is False
    # a different thread keeps its own budget
    assert thread_reply_allowed(state, "t2", now) is True


def test_thread_reply_cap_resets_next_day(monkeypatch):
    monkeypatch.setenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", "1")
    state: dict = {}
    day1 = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
    assert thread_reply_allowed(state, "t1", day1) is True
    assert thread_reply_allowed(state, "t1", day1) is False
    assert thread_reply_allowed(state, "t1", day2) is True


def test_thread_cap_default_is_two(monkeypatch):
    monkeypatch.delenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", raising=False)
    assert per_thread_cap() == 2


def test_thread_reply_cap_allows_unknown_thread():
    assert thread_reply_allowed({}, None) is True


def test_escalation_notice_names_sender_and_trigger():
    text = format_meeting_escalation(
        sender="Brendan Lon <brendan.lon@ellacheong.asia>",
        subject="RFQ - trade mark",
        thread="abc123",
        matched="are you available",
    )
    assert "Brendan Lon" in text
    assert "are you available" in text
    assert "abc123" in text
    # The notice must be explicit that nothing was sent.
    assert "NOT sent" in text
    assert len(text) < 2000


# ---- S4 matter cards + nudge loop -----------------------------------------
# The half of EMAIL_SEND_POLICY.md S4 that was never built: drafts accumulated
# with nothing surfacing them. On 2026-10-21 that left correspondents waiting
# five days and four law firms unanswered.


def test_route_for_sends_trademark_to_its_channel():
    from app.email_watch_policy import route_for

    assert (
        route_for("Request for Quotation - Trade Mark (BIONPACT)")
        == "1560756241070952478"
    )


def test_route_for_falls_back_to_escalation_channel():
    from app.email_watch_policy import escalation_channel, route_for

    assert route_for("hello there", "someone@example.com") == escalation_channel()


def test_matter_card_contains_the_four_decision_lines():
    from app.email_watch_policy import format_matter_card

    card = format_matter_card(
        subject="Re: BIONPACT",
        sender="a@b.com",
        received="2026-10-19",
        thread_id="t1",
    )
    for needle in (
        "**Matter:**",
        "**Deadline:**",
        "**Needs from Gary:**",
        "**Email thread:**",
    ):
        assert needle in card


def test_due_for_nudge_false_before_threshold():
    from datetime import datetime, timedelta, timezone

    from app.email_watch_policy import due_for_nudge

    now = datetime.now(timezone.utc)
    rec = {"status": "awaiting-decision", "at": (now - timedelta(days=1)).isoformat()}
    assert due_for_nudge(rec, now) is False


def test_due_for_nudge_true_past_threshold():
    from datetime import datetime, timedelta, timezone

    from app.email_watch_policy import due_for_nudge

    now = datetime.now(timezone.utc)
    rec = {"status": "awaiting-decision", "at": (now - timedelta(days=5)).isoformat()}
    assert due_for_nudge(rec, now) is True


def test_due_for_nudge_false_once_nudged():
    from datetime import datetime, timedelta, timezone

    from app.email_watch_policy import due_for_nudge

    now = datetime.now(timezone.utc)
    rec = {
        "status": "awaiting-decision",
        "at": (now - timedelta(days=9)).isoformat(),
        "nudged_at": now.isoformat(),
    }
    assert due_for_nudge(rec, now) is False


def test_matter_card_not_posted_for_a_live_send(monkeypatch):
    """A sent reply needs no card. Only held/draft dispositions create matters."""
    from app.email_inbox_watch import EmailInboxWatch

    posted = []
    monkeypatch.setattr(
        "app.email_inbox_watch.notify_discord",
        lambda text, channel=None: posted.append(text) or True,
    )
    watch = EmailInboxWatch.__new__(EmailInboxWatch)
    watch._state = {}
    watch._save = lambda: None
    watch._get_state = lambda: watch._state
    watch._post_matter_card({"thread_id": "t1", "subject": "s", "from": "f"}, ["live"])
    assert posted == []
    assert "matters" not in watch._state


def test_matter_card_posted_and_recorded_when_held(monkeypatch):
    from app.email_inbox_watch import EmailInboxWatch

    posted = []
    monkeypatch.setattr(
        "app.email_inbox_watch.notify_discord",
        lambda text, channel=None: posted.append(text) or True,
    )
    watch = EmailInboxWatch.__new__(EmailInboxWatch)
    watch._state = {}
    watch._save = lambda: None
    watch._get_state = lambda: watch._state
    watch._post_matter_card(
        {"thread_id": "t1", "subject": "s", "from": "f"}, ["draft_only"]
    )
    assert len(posted) == 1
    assert watch._state["matters"]["t1"]["status"] == "awaiting-decision"
