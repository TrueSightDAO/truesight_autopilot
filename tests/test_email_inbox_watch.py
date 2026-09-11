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


def test_allowed_tools_are_only_gmail_surface():
    assert ALLOWED_TOOLS == frozenset(
        {"gmail_search", "gmail_read_message", "gmail_send", "gmail_create_draft"}
    )


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


def test_state_write_is_atomic(monkeypatch, tmp_path):
    w = _watch(monkeypatch, tmp_path)
    w._dispatch_tool("gmail_search", {"query": "x"}, {"thread": "t1"}, False)
    assert (tmp_path / "state.json").exists()
    assert not list(tmp_path.glob(".email-watch-*.tmp"))  # no temp left behind


# --------------------------------------------------------------------------
# Unit 4 - lifespan wiring (guarded; OFF by default)
# --------------------------------------------------------------------------


def test_email_watch_enabled_defaults_false():
    """The loop must ship inert: EMAIL_WATCH_ENABLED default False."""
    from app.config import Settings

    # A fresh Settings with no env override for the flag must be False.
    assert Settings().email_watch_enabled is False


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
