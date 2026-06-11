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
    # Registry lookup misses -> non-empty generic hint, never empty.
    monkeypatch.setattr(ta, "_handoff_plan_for_thread", lambda tid: None)
    out = ta._handoff_prefix(777)
    assert out and "HANDOFF_MANIFEST.md" in out and "lack context" in out


def test_handoff_prefix_empty_outside_topic():
    assert ta._handoff_prefix(None) == ""
    assert ta._handoff_prefix(0) == ""


# --- post_to_telegram_topic (post into an EXISTING thread) ---
from app.tools import telegram_post as tp


def test_post_requires_message():
    out = tp.post_to_telegram_topic(message="  ", thread_id=1955, chat_id="-1001234567890")
    assert out["status"] == "error" and "message" in out["reason"]


def test_post_requires_numeric_thread_id():
    out = tp.post_to_telegram_topic(message="hi", thread_id="not-a-number", chat_id="-1001234567890")
    assert out["status"] == "error" and "thread_id" in out["reason"]


def test_post_missing_token_errors(monkeypatch):
    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "", raising=False)
    out = tp.post_to_telegram_topic(message="hi", thread_id=1955, chat_id="-1001234567890")
    assert out["status"] == "error" and "TELEGRAM_BOT_API_KEY" in out["reason"]


def test_post_no_target_group_errors(monkeypatch):
    monkeypatch.setattr(tp.settings, "telegram_bot_api_key", "dummy", raising=False)
    monkeypatch.setattr(tp.settings, "telegram_home_group_id", "", raising=False)
    out = tp.post_to_telegram_topic(message="hi", thread_id=1955, session_id="pub:web-xyz")
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
