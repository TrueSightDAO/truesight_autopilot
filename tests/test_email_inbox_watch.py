"""Unit tests for the email inbox watch (app/email_inbox_watch.py).

Covers the Unit 1 skeleton (query scope, lazy import, poll count) AND the Unit 2
security-policy enforcement at the dispatcher level: reduced tool surface
(rule 1), escalation hold (rule 3), first-contact draft-only (rule 4), secret
redaction (rule 5), send caps (rule 6), audit trail (rule 7), the dedicated
write gate (EMAIL_WATCH_ENABLE_SENDS), and per-thread idempotency.

SAFETY: every test stubs the tool registry / handler so NO test can ever reach a
real Gmail send. (2026-09-09: an env-timing bug sent 5 real test emails; these
tests deliberately make that impossible.)
"""

from __future__ import annotations

import json

from app import email_inbox_watch as m
from app.email_inbox_watch import (
    ALLOWED_TOOLS,
    ESCALATION_PATTERN,
    EmailInboxWatch,
    INBOX_QUERY,
    SEND_DAILY_CAP,
    SEND_HOURLY_CAP,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _watch(monkeypatch, tmp_path) -> EmailInboxWatch:
    monkeypatch.setattr(m, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(m, "_STATE_FILE", tmp_path / "state.json")
    w = EmailInboxWatch()
    w._state = None
    # Defensive: no test may hit a real handler.
    monkeypatch.setattr(
        w, "_run_handler", lambda name, args: json.dumps({"status": "ok", "tool": name})
    )
    return w


def _ok_search(n: int) -> str:
    return json.dumps(
        {
            "status": "ok",
            "result_count": n,
            "results": [
                {
                    "id": f"m{i}",
                    "thread_id": f"t{i}",
                    "from": "sender@example.com",
                    "subject": f"Subject {i}",
                }
                for i in range(n)
            ],
        }
    )


def _ok_read() -> str:
    return json.dumps(
        {
            "status": "ok",
            "id": "m1",
            "thread_id": "t1",
            "headers": {
                "from": "a@b.com",
                "to": "admin+sophia@truesight.me",
                "subject": "hi",
                "message_id": "<abc@b.com>",
            },
            "body": "hello",
        }
    )


# --------------------------------------------------------------------------
# Unit 1 - skeleton
# --------------------------------------------------------------------------


def test_inbox_query_is_plus_alias_scoped():
    assert "to:admin+sophia@truesight.me" in INBOX_QUERY
    assert "is:unread" in INBOX_QUERY


def test_inbox_query_covers_both_mailboxes_and_excludes_promotions():
    """community@ must be watched, and promotions must not crowd out the window.

    The Guilherme export-prequalification email and the Brandz Connect follow-up both
    landed on community@agroverse.shop and were never seen. Promo exclusion matters
    because that mailbox gets steady TikTok Shop / retail-blast volume that would
    otherwise fill max_results before any real correspondent is reached.
    """
    assert "admin+sophia@truesight.me" in m.INBOX_QUERY
    assert "community@agroverse.shop" in m.INBOX_QUERY
    assert "is:unread" in m.INBOX_QUERY
    assert "-category:promotions" in m.INBOX_QUERY
    # must stay a single parenthesised OR group, not two loose clauses
    assert "(to:" in m.INBOX_QUERY


def test_inbox_query_is_env_overridable(monkeypatch):
    """An operator can narrow or widen the watched mailboxes without a code change."""
    import importlib

    monkeypatch.setenv("EMAIL_WATCH_INBOX_QUERY", "to:x@y.com is:unread")
    try:
        reloaded = importlib.reload(m)
        assert reloaded.INBOX_QUERY == "to:x@y.com is:unread"
    finally:
        monkeypatch.delenv("EMAIL_WATCH_INBOX_QUERY", raising=False)
        importlib.reload(m)


def test_allowed_tools_are_email_plus_calendar_surface():
    # EMAIL_SEND_POLICY.md: the gmail surface plus calendar invites, nothing else.
    assert ALLOWED_TOOLS == frozenset(
        {
            "gmail_search",
            "gmail_read_message",
            "gmail_send",
            "gmail_create_draft",
            "calendar_list_events",
            "calendar_create_event",
        }
    )
    # No shell, no PR, no file or money surface may ever leak in.
    for forbidden in ("ssh_run", "merge_pr", "git_push_changes", "submit_contribution"):
        assert forbidden not in ALLOWED_TOOLS


def test_poll_once_counts_unread(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "_search", lambda: lambda **kw: _ok_search(3))
    monkeypatch.setattr(w, "_triage", lambda row: None)
    assert w.poll_once() == 3


def test_poll_once_search_error_returns_zero(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(
        w,
        "_search",
        lambda: lambda **kw: json.dumps({"status": "error", "reason": "x"}),
    )
    assert w.poll_once() == 0


# --------------------------------------------------------------------------
# rule 1 - reduced tool surface
# --------------------------------------------------------------------------


def test_dispatch_refuses_out_of_surface_tool(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    result, disp, mode = w._dispatch_tool(
        "ssh_run", {"cmd": "rm -rf /"}, {"thread": "t1"}, False
    )
    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert "outside the email-triage tool surface" in payload["reason"]
    assert disp is None and mode is None


def test_tool_schemas_are_only_allowed_surface(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)

    class _Spec:
        def __init__(self, name):
            self.name = name

        def to_openai_schema(self):
            return {"type": "function", "function": {"name": self.name}}

    monkeypatch.setattr(
        w,
        "_registry",
        lambda: {
            n: _Spec(n) for n in ["gmail_send", "ssh_run", "merge_pr", "gmail_search"]
        },
    )
    names = {s["function"]["name"] for s in w._tool_schemas()}
    assert names == {"gmail_send", "gmail_search"}


def test_dispatch_pins_account(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    captured = {}

    def fake_run(name, args):
        captured.update(args)
        return json.dumps({"status": "ok", "results": []})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    w._dispatch_tool("gmail_search", {"query": "from:x@y.com"}, {"thread": "t1"}, False)
    assert captured.get("account") == "admin"


# --------------------------------------------------------------------------
# rule 3 - escalation
# --------------------------------------------------------------------------


def test_escalation_pattern_matches_bec_framing():
    assert ESCALATION_PATTERN.search("please wire transfer $5000 today")
    assert ESCALATION_PATTERN.search("update your bank details")
    assert ESCALATION_PATTERN.search("I need a password reset")
    assert not ESCALATION_PATTERN.search("can we schedule a call tuesday")


def test_dispatch_escalation_outbound_holds(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "ceo@x.com", "subject": "wire", "body": "please wire transfer 5000"},
        {"thread": "t1"},
        False,
    )
    assert json.loads(result)["status"] == "held"
    assert disp == "hold_cc_gary"
    assert mode == "held"


# --------------------------------------------------------------------------
# rule 4 - first contact -> draft
# --------------------------------------------------------------------------


def test_first_contact_send_downgrades_to_draft(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setenv("EMAIL_WATCH_ENABLE_SENDS", "1")
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    monkeypatch.setattr(m.settings, "dry_run", False)
    called = {}

    def fake_run(name, args):
        called["name"] = name
        return json.dumps({"status": "ok", "draft_id": "d1"})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "new@x.com", "subject": "yo", "body": "hey"},
        {"thread": "t1"},
        True,
    )
    assert called["name"] == "gmail_create_draft"  # went to the DRAFT handler
    assert mode == "draft_only"


# --------------------------------------------------------------------------
# tier 0 - machine senders are never emailed (EMAIL_SEND_POLICY.md S1)
# --------------------------------------------------------------------------


def test_tier0_machine_sender_never_sends(monkeypatch, tmp_path):
    """A send to a machine mailbox must be a DRAFT even for a known sender.

    10 of the first 16 queued replies were to exactly this class (PyPI x4, npm,
    hushed verification, internal digital-signature verification mail).
    """
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    monkeypatch.setattr(m.settings, "dry_run", False)
    called = {}

    def fake_run(name, args):
        called["name"] = name
        return json.dumps({"status": "ok", "draft_id": "d1"})

    monkeypatch.setattr(w, "_run_handler", fake_run)

    for addr in (
        "noreply@pypi.org",
        "no-reply@github.com",
        "verify@hushed.com",
        "mailer-daemon@example.com",
        "notifications@npmjs.com",
    ):
        called.clear()
        _result, _disp, mode = w._dispatch_tool(
            "gmail_send",
            {"to": addr, "subject": "re", "body": "thanks"},
            {"thread": "t1"},
            False,  # NOT first contact - tier0 must still downgrade
        )
        assert called.get("name") == "gmail_create_draft", addr
        assert mode == "tier0_draft_only", addr


def test_tier0_does_not_block_human_correspondents():
    """The tier-0 pattern must not catch ordinary professional addresses."""
    from app.email_inbox_watch import MACHINE_SENDER_PATTERN

    for addr in (
        "winnie.tham@amicalaw.com",
        "brendan.lon@ellacheong.asia",
        "salsabil@donburk.asia",
        "garyjob@truesight.me",
        "mendelgui5@gmail.com",
    ):
        assert not MACHINE_SENDER_PATTERN.search(addr), addr


# --------------------------------------------------------------------------
# calendar invites - on the surface, behind the same write gate
# --------------------------------------------------------------------------


def test_calendar_create_event_is_gated_like_a_send(monkeypatch, tmp_path):
    assert "calendar_create_event" in ALLOWED_TOOLS
    w = _watch(monkeypatch, tmp_path)
    called = {}

    def fake_run(name, args):
        called["name"] = name
        return json.dumps({"status": "ok", "event_id": "e1"})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    args = {
        "summary": "BIONPACT trade mark filing call",
        "start": "2026-10-21T16:00:00+08:00",
        "end": "2026-10-21T16:30:00+08:00",
        "attendees": ["garyjob@truesight.me", "brendan.lon@ellacheong.asia"],
    }

    # gate OFF -> stubbed, handler never reached
    monkeypatch.setattr(m, "SENDS_ENABLED", False)
    monkeypatch.setattr(m.settings, "dry_run", False)
    _r, _d, mode = w._dispatch_tool(
        "calendar_create_event", dict(args), {"thread": "t1"}, False
    )
    assert "name" not in called
    assert "gate-off" in (mode or "")

    # gate ON -> handler reached
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    _r, disp, _mode = w._dispatch_tool(
        "calendar_create_event", dict(args), {"thread": "t1"}, False
    )
    assert called.get("name") == "calendar_create_event"
    assert disp == "calendar_create_event"


# --------------------------------------------------------------------------
# rule 5 - redaction
# --------------------------------------------------------------------------


def test_redaction_applied_to_outbound_body(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setenv("EMAIL_WATCH_ENABLE_SENDS", "1")
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    monkeypatch.setattr(m.settings, "dry_run", False)
    captured = {}

    def fake_run(name, args):
        captured.update(args)
        return json.dumps({"status": "ok", "id": "s1"})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    w._dispatch_tool(
        "gmail_send",
        {"to": "a@b.com", "subject": "key", "body": "key AKIAABCDEFGHIJKLMNOP"},
        {"thread": "t1"},
        False,
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in captured["body"]
    assert "[REDACTED:AWS_ACCESS_KEY]" in captured["body"]


# --------------------------------------------------------------------------
# rule 6 - caps
# --------------------------------------------------------------------------


def test_send_cap_halts(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    w._state = {
        "handled_threads": {},
        "audit": [],
        "sent": {
            "hour": SEND_HOURLY_CAP,
            "day": SEND_DAILY_CAP,
            "hour_window": m._hour_key(),
            "day_window": m._day_key(),
        },
    }
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "a@b.com", "subject": "hi", "body": "hello"},
        {"thread": "t1"},
        False,
    )
    assert json.loads(result)["status"] == "halted"
    assert disp == "halted_cap"


def test_cap_window_resets(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    w._state = {
        "handled_threads": {},
        "audit": [],
        "sent": {
            "hour": SEND_HOURLY_CAP,
            "day": SEND_DAILY_CAP,
            "hour_window": "2020-01-01T00",
            "day_window": "2020-01-01",
        },
    }
    assert w._cap_exceeded() is False  # stale windows reset to 0


# --------------------------------------------------------------------------
# write gate (the incident fix)
# --------------------------------------------------------------------------


def test_write_gate_off_stubs_send_even_when_not_dry_run(monkeypatch, tmp_path):
    """The core safety invariant: ambient dry_run=False + gate OFF => no send."""
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(m.settings, "dry_run", False)
    monkeypatch.setattr(m, "SENDS_ENABLED", False)
    touched = {"run": False}

    def fake_run(name, args):
        touched["run"] = True
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "a@b.com", "subject": "hi", "body": "hello"},
        {"thread": "t1"},
        False,
    )
    assert json.loads(result)["status"] == "dry_run"
    assert disp == "would_gmail_send"
    assert "gate-off" in mode
    assert touched["run"] is False  # handler NEVER ran


def test_write_gate_dry_run_stubs_send(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(m.settings, "dry_run", True)
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "a@b.com", "subject": "hi", "body": "hello"},
        {"thread": "t1"},
        False,
    )
    assert json.loads(result)["status"] == "dry_run"
    assert "dry_run" in mode


# --------------------------------------------------------------------------
# rule 7 - audit + idempotency
# --------------------------------------------------------------------------


def test_audit_trail_written(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    w._dispatch_tool("gmail_search", {"query": "x"}, {"thread": "t1"}, False)
    w._dispatch_tool(
        "gmail_send",
        {"to": "a@b.com", "subject": "hi", "body": "hello"},
        {"thread": "t1"},
        False,
    )
    state = json.loads((tmp_path / "state.json").read_text())
    tools = {row["tool"] for row in state["audit"]}
    assert {"gmail_search", "gmail_send"} <= tools


def test_thread_triaged_once(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(w, "_search", lambda: lambda **kw: _ok_search(1))
    monkeypatch.setattr(w, "_read", lambda: lambda message_id, account: _ok_read())

    class _LLM:
        def __init__(self):
            self.calls = 0

        def chat(self, system, messages, tools=None, **kw):
            self.calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "routine",
                            "tool_calls": None,
                        }
                    }
                ]
            }

        def extract_text(self, c):
            return "routine"

    llm = _LLM()
    monkeypatch.setattr(w, "_llm_client", lambda: llm)
    assert w.poll_once() == 1
    assert "t0" in w._get_state()["handled_threads"]
    assert w.poll_once() == 1
    assert llm.calls == 1  # not re-triaged


def test_thread_retriaged_when_new_message_arrives(monkeypatch, tmp_path):
    """A new reply in an already-handled thread must be re-triaged.

    Previously handled_threads was keyed on thread id alone, so later replies were
    invisible: thread 1a1467c088cb883c was never triaged and a 2026-10-19 Caroline
    Berube reply sat unread in a thread marked handled on 2026-10-17.
    """
    w = _watch(monkeypatch, tmp_path)
    calls = {"n": 0}

    def _search(**kw):
        calls["n"] += 1
        mid = "m1" if calls["n"] >= 3 else "m0"
        return json.dumps(
            {
                "status": "ok",
                "result_count": 1,
                "results": [
                    {
                        "id": mid,
                        "thread_id": "t0",
                        "from": "a@b.com",
                        "subject": "s",
                    }
                ],
            }
        )

    monkeypatch.setattr(w, "_search", lambda: _search)
    monkeypatch.setattr(w, "_read", lambda: lambda message_id, account: _ok_read())

    class _LLM:
        def __init__(self):
            self.calls = 0

        def chat(self, system, messages, tools=None, **kw):
            self.calls += 1
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "routine",
                            "tool_calls": None,
                        }
                    }
                ]
            }

        def extract_text(self, c):
            return "routine"

    llm = _LLM()
    monkeypatch.setattr(w, "_llm_client", lambda: llm)

    # first sight of m0 -> triaged
    w.poll_once()
    assert llm.calls == 1
    # same message id again -> NOT re-triaged (idempotency preserved)
    w.poll_once()
    assert llm.calls == 1
    # a NEW message id in the same thread -> re-triaged
    w.poll_once()
    assert llm.calls == 2
    assert w._get_state()["handled_threads"]["t0"]["triage_count"] == 2


def test_state_write_is_atomic(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    w._dispatch_tool("gmail_search", {"query": "x"}, {"thread": "t1"}, False)
    assert (tmp_path / "state.json").exists()
    assert not list(tmp_path.glob(".email-watch-*.tmp"))  # no temp left behind


# --------------------------------------------------------------------------
# Unit 4 - lifespan wiring (guarded; OFF by default)
# --------------------------------------------------------------------------


def test_email_watch_enabled_defaults_false(monkeypatch):
    """The loop must ship inert: EMAIL_WATCH_ENABLED default False.

    Hermetic: ``_env_file=None`` only stops pydantic-settings reading the .env
    FILE -- it still reads ``os.environ``. On the live box the service exports
    the .env keys into the process environment, so this test failed there while
    passing on a clean CI runner (no .env, nothing exported). The var is now
    scrubbed explicitly so the assertion holds on any host.
    """
    from app.config import Settings

    monkeypatch.delenv("EMAIL_WATCH_ENABLED", raising=False)
    assert Settings(_env_file=None).email_watch_enabled is False


def test_start_helper_is_noop_when_disabled(monkeypatch):
    """When disabled, the helper schedules nothing and returns False."""
    import app.main as app_main

    scheduled = []
    monkeypatch.setattr(app_main.settings, "email_watch_enabled", False)

    def _capture(c):
        c.close()
        scheduled.append(c)

    monkeypatch.setattr(app_main.asyncio, "create_task", _capture)
    assert app_main._start_email_watch_if_enabled() is False
    assert scheduled == []


def test_start_helper_schedules_loop_when_enabled(monkeypatch):
    """When enabled, the helper schedules the loop and returns True."""
    import app.main as app_main

    scheduled = []
    monkeypatch.setattr(app_main.settings, "email_watch_enabled", True)

    def _capture(c):
        c.close()
        scheduled.append(c)

    monkeypatch.setattr(app_main.asyncio, "create_task", _capture)
    assert app_main._start_email_watch_if_enabled() is True
    assert len(scheduled) == 1
    assert app_main.email_watch is not None


def test_enabling_loop_does_not_enable_sends(monkeypatch, tmp_path):
    """Unit 4 point: enabling the loop must NOT turn on sending by itself.

    With the loop enabled but EMAIL_WATCH_ENABLE_SENDS unset, an outbound
    gmail_send is still intercepted as a dry_run/gate-off stub.
    """
    w = _watch(monkeypatch, tmp_path)
    # Loop "enabled" conceptually, but ambient gate off + dry_run off is the
    # ungated danger case - the dedicated flag must still hold the line.
    monkeypatch.setattr(m, "SENDS_ENABLED", False)
    monkeypatch.setattr(m.settings, "dry_run", False)
    called = {"n": 0}
    monkeypatch.setattr(
        w, "_run_handler", lambda name, args: called.__setitem__("n", called["n"] + 1)
    )
    result, disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "known@x.com", "subject": "s", "body": "b"},
        {"thread": "t1"},
        False,  # not first contact
    )
    payload = json.loads(result)
    assert payload["status"] == "dry_run"
    assert payload["would"] == "gmail_send"
    assert disp == "would_gmail_send"
    assert mode == "live|gate-off"
    assert called["n"] == 0  # handler NEVER invoked


# --------------------------------------------------------------------------
# criteria 2/3/6 - meeting hold, per-thread cap, pause switch (wired gates)
# --------------------------------------------------------------------------


def _live(watch, monkeypatch):
    """Make the write gate live so a real handler call is reachable."""
    monkeypatch.setattr(m, "SENDS_ENABLED", True)
    monkeypatch.setattr(m.settings, "dry_run", False)
    return watch


def test_meeting_request_forces_draft_not_send(monkeypatch, tmp_path):
    """AC2: `meeting_requested` must hold the reply as a DRAFT.

    This is the gap Gary found: the policy function existed and was tested,
    but _dispatch_tool never consulted it, so a meeting ask went to the model.
    """
    w = _live(_watch(monkeypatch, tmp_path), monkeypatch)
    called = {}

    def fake_run(name, args):
        called["name"] = name
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(w, "_run_handler", fake_run)
    _result, _disp, mode = w._dispatch_tool(
        "gmail_send",
        {"to": "c@x.com", "subject": "Re: chat", "body": "sure"},
        {"thread": "t1", "meeting_requested": True, "meeting_trigger": "let's meet"},
        False,
    )
    assert called["name"] == "gmail_create_draft"  # held, not sent
    assert mode == "meeting_draft_only"


def test_meeting_request_detected_from_inbound_body(monkeypatch, tmp_path):
    """AC2: detection happens on the INBOUND body inside _triage."""
    w = _watch(monkeypatch, tmp_path)
    posted = {}

    class FakeClient:
        def chat(self, system, messages, tools=None):
            return {"choices": [{"message": {"content": "noted"}}]}  # no tool calls

    monkeypatch.setattr(w, "_llm_client", lambda: FakeClient())
    monkeypatch.setattr(
        w,
        "_read",
        lambda: (
            lambda **kw: json.dumps(
                {
                    "status": "ok",
                    "thread_id": "t9",
                    "headers": {
                        "from": "c@x.com",
                        "subject": "Coffee?",
                        "to": "admin@truesight.me",
                    },
                    "body": "Are you free for a call next week?",
                }
            )
        ),
    )
    monkeypatch.setattr(
        w,
        "_dispatch_tool",
        lambda name, args, ctx, fc: ('{"status":"ok"}', None, None),
    )
    import app.email_inbox_watch as mod

    monkeypatch.setattr(
        mod,
        "notify_discord",
        lambda text, channel=None: posted.setdefault("text", text) or True,
    )
    w._triage({"id": "m1", "thread_id": "t9"})
    assert "text" in posted, "meeting request must post an escalation notice"
    assert "Meeting request" in posted["text"]


def test_per_thread_cap_blocks_third_send(monkeypatch, tmp_path):
    """AC3: per-thread daily cap must be enforced at real send time."""
    w = _live(_watch(monkeypatch, tmp_path), monkeypatch)
    monkeypatch.setenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", "2")
    sent = []
    monkeypatch.setattr(
        w, "_run_handler", lambda n, a: sent.append(n) or json.dumps({"status": "ok"})
    )
    args = {"to": "known@x.com", "subject": "s", "body": "b"}
    ctx = {"thread": "cap-thread"}
    r1 = w._dispatch_tool("gmail_send", dict(args), ctx, False)
    r2 = w._dispatch_tool("gmail_send", dict(args), ctx, False)
    r3 = w._dispatch_tool("gmail_send", dict(args), ctx, False)
    assert r1[1] == "gmail_send" and r2[1] == "gmail_send"
    assert r3[1] == "halted_thread_cap"  # third is held
    assert sent.count("gmail_send") == 2  # handler ran only twice


def test_per_thread_cap_not_consumed_by_dry_run(monkeypatch, tmp_path):
    """Reserve-on-check must NOT burn a slot when the write is gated off.

    If the cap were checked before the write gate, every dry-run tick would
    silently consume the day's quota and real replies would be held.
    """
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "SENDS_ENABLED", False)
    monkeypatch.setattr(m.settings, "dry_run", True)
    monkeypatch.setenv("EMAIL_WATCH_PER_THREAD_DAILY_CAP", "2")
    ctx = {"thread": "dry-thread"}
    for _ in range(5):
        w._dispatch_tool(
            "gmail_send", {"to": "k@x.com", "subject": "s", "body": "b"}, ctx, False
        )
    assert w._get_state().get("thread_replies", {}).get("dry-thread") is None


def test_pause_file_makes_tick_inert(monkeypatch, tmp_path):
    """AC6: a pause file must stop the tick before it even searches."""
    w = _watch(monkeypatch, tmp_path)
    searched = []
    monkeypatch.setattr(
        w, "_search", lambda: lambda **kw: searched.append(1) or _ok_search(3)
    )
    assert w.poll_once() == 3 and searched  # baseline: it searches

    (tmp_path / "EMAIL_WATCH_PAUSE").write_text("halted by governor\n")
    monkeypatch.setenv("EMAIL_WATCH_PAUSE_FILE", str(tmp_path / "EMAIL_WATCH_PAUSE"))
    searched.clear()
    assert w.poll_once() == 0
    assert searched == [], "paused tick must not touch Gmail at all"


def test_pause_env_var_makes_tick_inert(monkeypatch, tmp_path):
    """AC6: the env-var form of the kill switch works too."""
    w = _watch(monkeypatch, tmp_path)
    monkeypatch.setenv("EMAIL_WATCH_PAUSE", "1")
    assert w.poll_once() == 0
