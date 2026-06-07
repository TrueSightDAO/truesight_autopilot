"""Tests for create_telegram_topic chat-id resolution + guard paths."""
from app.tools import telegram_topic as tt


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
