"""Unit tests for the attention-watchdog ask heuristics (pure functions)."""

from app.attention_watchdog import chat_deep_link, classify_text, should_track

# ── classify_text ───────────────────────────────────────────────────────────


def test_question_mark_is_ask():
    ask, dated = classify_text("Are we still on for the serving?")
    assert ask and not dated or dated  # '?' guarantees ask; date optional


def test_dated_ask_june_12_style():
    # The message shape that cancelled the June 12 serving.
    ask, dated = classify_text("Can you confirm the cacao serving on June 12")
    assert ask and dated


def test_ordinal_date_detected():
    ask, dated = classify_text("does the 12th still work for you")
    assert ask and dated


def test_time_of_day_detected():
    ask, dated = classify_text("we'd need you there by 3:30pm")
    assert ask and dated


def test_weekday_detected():
    ask, dated = classify_text("let me know if Friday works")
    assert ask and dated


def test_asky_phrasing_without_question_mark():
    ask, dated = classify_text("please confirm when you get a chance")
    assert ask and not dated


def test_plain_statement_not_tracked():
    ask, _ = classify_text("Great seeing you at the market today!")
    assert not ask


def test_empty_text_not_tracked():
    assert classify_text("") == (False, False)
    assert classify_text(None) == (False, False)


# ── should_track gate ───────────────────────────────────────────────────────


def test_dm_with_ask_tracks():
    track, dated = should_track(
        is_private=True,
        mentioned=False,
        sender_is_bot=False,
        is_broadcast=False,
        text="Can you confirm June 12?",
    )
    assert track and dated


def test_group_without_mention_ignored():
    track, _ = should_track(
        is_private=False,
        mentioned=False,
        sender_is_bot=False,
        is_broadcast=False,
        text="Can anyone confirm June 12?",
    )
    assert not track


def test_group_with_mention_tracks():
    track, _ = should_track(
        is_private=False,
        mentioned=True,
        sender_is_bot=False,
        is_broadcast=False,
        text="can you make the 12th?",
    )
    assert track


def test_bot_sender_ignored():
    track, _ = should_track(
        is_private=True,
        mentioned=True,
        sender_is_bot=True,
        is_broadcast=False,
        text="Will you confirm by Friday?",
    )
    assert not track


def test_broadcast_channel_ignored():
    track, _ = should_track(
        is_private=False,
        mentioned=True,
        sender_is_bot=False,
        is_broadcast=True,
        text="RSVP by June 12?",
    )
    assert not track


def test_dm_smalltalk_ignored():
    track, _ = should_track(
        is_private=True,
        mentioned=False,
        sender_is_bot=False,
        is_broadcast=False,
        text="thanks again, that was lovely",
    )
    assert not track


# ── deep links ──────────────────────────────────────────────────────────────


def test_supergroup_deep_link():
    assert chat_deep_link(-1001234567890, 42, None) == "https://t.me/c/1234567890/42"


def test_public_username_link():
    assert chat_deep_link(123, 7, "somegroup") == "https://t.me/somegroup/7"


def test_plain_dm_no_link():
    assert chat_deep_link(123456, 7, None) == ""
