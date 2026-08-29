"""Tests for create_telegram_topic chat-id resolution + guard paths."""

from app import telegram_adapter as ta
from app.tools import telegram_topic as tt

_REG = """
| Date | Handoff | Plan file | Topic | thread_id | session_id | Status |
|------|---------|-----------|-------|-----------|------------|--------|
| 2026-06-09 | Subs Phase 1 | `CHOCOLATE_SUBSCRIPTION_PLAN.md` | [t](x) | 1939 | `tg:-1003919341801:1939` | **active — parked GO-ready** |
| 2026-06-09 | ~~initial~~ | `CHOCOLATE_SUBSCRIPTION_PLAN.md` | [t](x) | 1924 | `tg:-1003919341801:1924` | **SUPERSEDED by 1939** |
"""


def test_parse_handoff_plan_matches_active_thread():
    assert ta._parse_handoff_plan(_REG, 1939) == "CHOCOLATE_SUBSCRIPTION_PLAN.md"


def test_parse_handoff_plan_skips_superseded_row():
    # 1924 row references the plan but is not active -> no match (and the
    # "by 1939" mention in its status must not false-match thread 1939).
    assert ta._parse_handoff_plan(_REG, 1924) is None


def test_parse_handoff_plan_unknown_thread_is_none():
    assert ta._parse_handoff_plan(_REG, 4242) is None


def test_handoff_prefix_generic_fallback_when_no_plan(monkeypatch):
    # Registry lookup misses: the generic hint now fires ONLY on a go-signal /
    # plan reference (2026-06-12) — a normal chat message gets no handoff noise.
    # Patches _handoff_plan_and_auto_start_for_thread — the function _handoff_prefix
    # actually calls since the 2026-07-21 Auto-start refactor (_handoff_plan_for_thread
    # is now just a thin wrapper over it, no longer called directly here).
    monkeypatch.setattr(ta, "_handoff_plan_and_auto_start_for_thread", lambda tid: None)
    go = ta._handoff_prefix(777, "go for it")
    assert go and "HANDOFF_MANIFEST.md" in go and "lack context" in go
    assert ta._handoff_prefix(777, "just chatting") == ""  # normal chat → no prefix


def test_handoff_prefix_empty_outside_topic():
    assert ta._handoff_prefix(None) == ""
    assert ta._handoff_prefix(0) == ""


def test_handoff_prefix_auto_start_true_skips_go_signal_framing(monkeypatch):
    monkeypatch.setattr(
        ta, "_handoff_plan_and_auto_start_for_thread", lambda tid: ("AUTO.md", True)
    )
    prefix = ta._handoff_prefix(555, "anything")
    assert "PRE-AUTHORIZED" in prefix
    assert "do NOT wait for a governor go-signal" in prefix
    assert "always-stop gate" in prefix  # still calls out §5c gates apply


def test_handoff_prefix_auto_start_false_keeps_go_signal_framing(monkeypatch):
    monkeypatch.setattr(
        ta, "_handoff_plan_and_auto_start_for_thread", lambda tid: ("MANUAL.md", False)
    )
    prefix = ta._handoff_prefix(556, "anything")
    assert "PRE-AUTHORIZED" not in prefix
    assert 'the governor\'s full authorization' in prefix


# --- post_to_telegram_topic (post into an EXISTING thread) ---
from app.tools import telegram_post as tp  # noqa: E402 — grouped with its tests below


def test_post_requires_message():
    out = tp.post_to_telegram_topic(
        message="  ", thread_id=1955, chat_id="-1001234567890"
    )
    assert out["status"] == "error" and "message" in out["reason"]


def test_post_requires_numeric_thread_id():
    out = tp.post_to_telegram_topic(
        message="hi", thread_id="not-a-number", chat_id="-1001234567890"
    )
    assert out["status"] == "error" and "thread_id" in out["reason"]


def test_post_missing_token_errors(monkeypatch):
    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "", raising=False)
    out = tp.post_to_telegram_topic(
        message="hi", thread_id=1955, chat_id="-1001234567890"
    )
    assert out["status"] == "error" and "TELEGRAM_BOT_API_KEY" in out["reason"]


def test_post_no_target_group_errors(monkeypatch):
    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(tp.settings, "telegram_home_group_id", "", raising=False)
    out = tp.post_to_telegram_topic(
        message="hi", thread_id=1955, session_id="pub:web-xyz"
    )
    assert out["status"] == "error" and "chat_id" in out["reason"]


def test_chat_id_from_tg_session():
    assert tt._chat_id_from_session("abc123:tg:-1001234567890:42") == "-1001234567890"


def test_chat_id_from_non_tg_session_is_none():
    assert tt._chat_id_from_session("abc123:web-session-xyz") is None
    assert tt._chat_id_from_session(None) is None


def test_deep_link_supergroup():
    assert tt._deep_link("-1001234567890", 42) == "https://t.me/c/1234567890/42"


def test_deep_link_non_supergroup_blank():
    assert tt._deep_link("123456", 42) == ""


def test_missing_name_errors():
    out = tt.create_telegram_topic(name="  ")
    assert out["status"] == "error" and "name" in out["reason"]


def test_no_target_group_errors(monkeypatch):
    """No tg session + no home group configured -> actionable error, no API call."""
    monkeypatch.setattr(tt.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(tt.settings, "telegram_home_group_id", "", raising=False)
    out = tt.create_telegram_topic(name="Exec: X", session_id="pub:web-xyz")
    assert out["status"] == "error" and "TELEGRAM_HOME_GROUP_ID" in out["reason"]


def test_missing_token_errors(monkeypatch):
    monkeypatch.setattr(tt.settings, "telegram_bot_api_key", "", raising=False)
    out = tt.create_telegram_topic(name="Exec: X", chat_id="-1001234567890")
    assert out["status"] == "error" and "TELEGRAM_BOT_API_KEY" in out["reason"]


# ── resume_awaiting flag hooks (PR2) ──


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_post_flag_registers_message(monkeypatch):
    import app.resume_registry as rr

    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(
        tp.httpx,
        "post",
        lambda *a, **k: _FakeResp({"ok": True, "result": {"message_id": 555}}),
    )
    marked = []
    monkeypatch.setattr(
        rr,
        "mark_resume_awaiting",
        lambda mid, tid, text: marked.append((mid, tid, text)),
    )
    out = tp.post_to_telegram_topic(
        message="ready go",
        thread_id=15728,
        chat_id="-1003919341801",
        resume_awaiting=True,
    )
    assert out["status"] == "ok" and out["message_id"] == 555
    assert marked == [(555, 15728, "ready go")]


def test_post_auto_flags_resume_here_text(monkeypatch):
    """A post carrying 'RESUME HERE' is flagged even without resume_awaiting=True."""
    import app.resume_registry as rr

    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(
        tp.httpx,
        "post",
        lambda *a, **k: _FakeResp({"ok": True, "result": {"message_id": 557}}),
    )
    marked = []
    monkeypatch.setattr(
        rr,
        "mark_resume_awaiting",
        lambda mid, tid, text: marked.append((mid, tid, text)),
    )
    out = tp.post_to_telegram_topic(
        message="Dependency ready. 📌 RESUME HERE",
        thread_id=15728,
        chat_id="-1003919341801",
        resume_awaiting=False,
    )
    assert out["status"] == "ok"
    assert marked == [(557, 15728, "Dependency ready. 📌 RESUME HERE")]


def test_post_plain_text_also_flagged(monkeypatch):
    """Every post is resume-awaiting now (2026-08-29: dropped the RESUME HERE
    / resume_awaiting gate) — any positive emoji reaction on any of her
    messages should mean "continue", not just specially-marked ones."""
    import app.resume_registry as rr

    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(
        tp.httpx,
        "post",
        lambda *a, **k: _FakeResp({"ok": True, "result": {"message_id": 558}}),
    )
    marked = []
    monkeypatch.setattr(
        rr, "mark_resume_awaiting", lambda mid, tid, text: marked.append(mid)
    )
    tp.post_to_telegram_topic(
        message="plain update",
        thread_id=15728,
        chat_id="-1003919341801",
        resume_awaiting=False,
    )
    assert marked == [558]
