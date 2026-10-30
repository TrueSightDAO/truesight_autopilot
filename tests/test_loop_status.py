"""Tests for the pure loop-daemon classifier (app/loop_status.py)."""

from __future__ import annotations

import math

import pytest

from app.loop_status import (
    DEFAULT_AWAITING_FRESH_S,
    DEFAULT_TRACK_LEASE_S,
    DEFECT_STATES,
    MAX_TRUSTED_LEASE_S,
    STATE_AWAITING,
    STATE_AWAITING_STALE,
    STATE_BUSY,
    STATE_IDLE,
    STATE_LOST_GOAL,
    STATE_STALLED,
    Lease,
    SessionStatus,
    _prompt_superseded,
    awaiting_age,
    classify_session,
    lease_age,
    lease_expired,
    parse_awaiting,
    parse_tracks,
    parse_ts,
    summarize,
)

NOW = 1_793_192_500.0  # fixed clock


# ── parse_ts ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-10-28T12:57:41.105Z", 1_793_192_261.105),
        ("2026-10-28T12:57:41+00:00", 1_793_192_261.0),
        ("2026-10-28T12:57:41", 1_793_192_261.0),  # naive -> UTC
        (NOW, NOW),
        (int(NOW), float(int(NOW))),
        (str(NOW), NOW),
        (1_793_192_261_105, 1_793_192_261.105),  # milliseconds
    ],
)
def test_parse_ts_accepts_valid_forms(raw, expected):
    parsed = parse_ts(raw)
    assert parsed is not None
    assert parsed == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize(
    "raw",
    [None, True, False, "", "   ", "not-a-date", 0, -5, float("nan"), float("inf")],
)
def test_parse_ts_rejects_unusable(raw):
    assert parse_ts(raw) is None


def test_parse_ts_wrong_type_is_none():
    assert parse_ts({"a": 1}) is None
    assert parse_ts([1, 2]) is None


# ── parse_tracks ─────────────────────────────────────────────────────────────


def _track(**over):
    base = {
        "id": "PK:tg:-100:42",
        "label": "Telegram chat — Gary Teh",
        "track_type": "telegram_chat",
        "started_at": "2026-10-28T12:57:41.105Z",
        "last_heartbeat": "2026-10-28T12:57:41.105Z",
        "expected_max_duration_s": 120,
        "status": "running",
        "metadata": {"session_id": "PK:tg:-100:42"},
    }
    base.update(over)
    return base


def test_parse_tracks_reads_dict_form():
    leases = parse_tracks({"version": 1, "tracks": [_track()]})
    assert len(leases) == 1
    lease = leases[0]
    assert lease.session_id == "PK:tg:-100:42"
    assert lease.is_running
    assert lease.expected_max_duration_s == 120
    assert lease.started_at == pytest.approx(1_793_192_261.105)


def test_parse_tracks_reads_bare_list():
    assert len(parse_tracks([_track()])) == 1


def test_parse_tracks_skips_non_chat_tracks():
    leases = parse_tracks(
        [
            _track(track_type="deploy"),
            _track(track_type="email_poll", id="PK:email:1"),
            _track(),
        ]
    )
    assert [lease.session_id for lease in leases] == ["PK:tg:-100:42"]


def test_parse_tracks_falls_back_to_id():
    leases = parse_tracks([_track(metadata=None, id="PK:tg:-100:99")])
    assert leases[0].session_id == "PK:tg:-100:99"


def test_parse_tracks_skips_entries_without_identity():
    assert parse_tracks([_track(metadata=None, id=None)]) == []
    assert parse_tracks([_track(metadata={}, id="   ")]) == []


@pytest.mark.parametrize(
    "raw_lease,expected",
    [
        (None, DEFAULT_TRACK_LEASE_S),
        ("nonsense", DEFAULT_TRACK_LEASE_S),
        (0, DEFAULT_TRACK_LEASE_S),
        (-1, DEFAULT_TRACK_LEASE_S),
        (float("nan"), DEFAULT_TRACK_LEASE_S),
        (float("inf"), DEFAULT_TRACK_LEASE_S),
        # sentinel-style huge value must not mask a stall forever
        (1e18, DEFAULT_TRACK_LEASE_S),
        (90, 90.0),
    ],
)
def test_parse_tracks_clamps_untrusted_lease(raw_lease, expected):
    leases = parse_tracks([_track(expected_max_duration_s=raw_lease)])
    assert leases[0].expected_max_duration_s == expected


def test_parse_tracks_non_dict_payload_is_empty():
    assert parse_tracks(None) == []
    assert parse_tracks("garbage") == []
    assert parse_tracks({"tracks": "nope"}) == []
    assert parse_tracks({"no_tracks_key": []}) == []


def test_parse_tracks_skips_non_dict_entries():
    assert parse_tracks({"tracks": [None, 3, "x", _track()]}) == [
        Lease(
            session_id="PK:tg:-100:42",
            label="Telegram chat — Gary Teh",
            status="running",
            started_at=1_793_192_261.105,
            last_heartbeat=1_793_192_261.105,
            expected_max_duration_s=120.0,
        )
    ]


def test_status_is_normalised():
    leases = parse_tracks([_track(status="RUNNING")])
    assert leases[0].is_running


# ── lease age / expiry ───────────────────────────────────────────────────────


def _lease(**over):
    base = dict(
        session_id="PK:tg:-100:42",
        status="running",
        started_at=NOW - 300,
        last_heartbeat=NOW - 10,
        expected_max_duration_s=120.0,
    )
    base.update(over)
    return Lease(**base)


def test_lease_age_prefers_heartbeat():
    assert lease_age(_lease(), NOW) == pytest.approx(10.0)


def test_lease_age_falls_back_to_started_at():
    assert lease_age(_lease(last_heartbeat=None), NOW) == pytest.approx(300.0)


def test_lease_age_none_when_unknowable():
    assert lease_age(_lease(last_heartbeat=None, started_at=None), NOW) is None


def test_lease_age_never_negative_for_future_timestamps():
    assert lease_age(_lease(last_heartbeat=NOW + 500), NOW) == 0.0


def test_lease_expired_false_within_window():
    assert lease_expired(_lease(), NOW) is False


def test_lease_expired_true_beyond_window():
    assert lease_expired(_lease(last_heartbeat=NOW - 500), NOW) is True


def test_lease_expired_boundary_is_inclusive():
    # exactly at the lease edge is still alive; strictly beyond is stalled
    assert lease_expired(_lease(last_heartbeat=NOW - 120), NOW) is False
    assert lease_expired(_lease(last_heartbeat=NOW - 120.001), NOW) is True


def test_lease_expired_fails_closed_when_unknowable():
    """No timestamps -> report NOT expired: better a missed stall than a false page."""
    assert lease_expired(_lease(last_heartbeat=None, started_at=None), NOW) is False


# ── awaiting registries ──────────────────────────────────────────────────────


def test_parse_awaiting_maps_thread_and_channel():
    raw = {
        "1": {"thread_id": "42419", "text": "x", "ts": NOW - 60},
        "2": {"channel_id": "1562138", "text": "y", "ts": NOW - 30},
    }
    assert parse_awaiting(raw, now=NOW) == {"42419": NOW - 60, "1562138": NOW - 30}


def test_parse_awaiting_keeps_freshest_per_identity():
    raw = {
        "1": {"thread_id": "42", "ts": NOW - 500},
        "2": {"thread_id": "42", "ts": NOW - 100},
    }
    assert parse_awaiting(raw, now=NOW) == {"42": NOW - 100}


def test_parse_awaiting_skips_entries_without_ts():
    raw = {"1": {"thread_id": "42", "text": "no ts"}, "2": {"thread_id": "43"}}
    assert parse_awaiting(raw, now=NOW) == {}


def test_parse_awaiting_skips_entries_without_identity():
    assert parse_awaiting({"1": {"text": "x", "ts": NOW}}, now=NOW) == {}


@pytest.mark.parametrize("raw", [None, "junk", 7, []])
def test_parse_awaiting_non_dict_is_empty(raw):
    assert parse_awaiting(raw, now=NOW) == {}


def test_awaiting_age_matches_telegram_suffix():
    age = awaiting_age(
        "PK:tg:-1003919341801:42419",
        telegram_awaiting={"42419": NOW - 120},
        discord_awaiting={},
        now=NOW,
    )
    assert age == pytest.approx(120.0)


def test_awaiting_age_matches_discord_suffix():
    age = awaiting_age(
        "PK:dc:923008087315587072:1562138999265558618",
        telegram_awaiting={},
        discord_awaiting={"1562138999265558618": NOW - 45},
        now=NOW,
    )
    assert age == pytest.approx(45.0)


def test_awaiting_age_requires_full_suffix_not_substring():
    """A thread id embedded mid-key must not match (no accidental prefixes)."""
    age = awaiting_age(
        "PK:tg:-100:142419",
        telegram_awaiting={"42419": NOW - 10},
        discord_awaiting={},
        now=NOW,
    )
    assert age is None


def test_awaiting_age_rejects_stale_entries():
    age = awaiting_age(
        "PK:tg:-100:42",
        telegram_awaiting={"42": NOW - (DEFAULT_AWAITING_FRESH_S + 60)},
        discord_awaiting={},
        now=NOW,
    )
    assert age is None


def test_awaiting_age_accepts_entry_just_inside_window():
    age = awaiting_age(
        "PK:tg:-100:42",
        telegram_awaiting={"42": NOW - (DEFAULT_AWAITING_FRESH_S - 1)},
        discord_awaiting={},
        now=NOW,
    )
    assert age is not None


def test_awaiting_age_picks_smallest():
    age = awaiting_age(
        "PK:tg:-100:42",
        telegram_awaiting={"42": NOW - 400},
        discord_awaiting={"42": NOW - 30},
        now=NOW,
    )
    assert age == pytest.approx(30.0)


def test_awaiting_age_empty_session_is_none():
    assert (
        awaiting_age("", telegram_awaiting={"42": NOW}, discord_awaiting={}, now=NOW)
        is None
    )


# ── classification ───────────────────────────────────────────────────────────


def _classify(**over):
    kwargs = dict(
        session_id="PK:tg:-100:42",
        lease=None,
        awaiting_age_s=None,
        open_goal_text=None,
        now=NOW,
        auto_advance=True,
        label="Telegram chat — Gary Teh",
    )
    kwargs.update(over)
    return classify_session(**kwargs)


def test_classify_busy_when_lease_healthy():
    status = _classify(lease=_lease())
    assert status.state == STATE_BUSY
    assert status.is_defect is False
    assert status.age_s == pytest.approx(10.0)


def test_classify_stalled_when_lease_blown():
    status = _classify(lease=_lease(last_heartbeat=NOW - 900))
    assert status.state == STATE_STALLED
    assert status.is_defect is True
    assert "900s" in status.reason


def test_classify_stalled_when_no_lease_timestamp():
    status = _classify(lease=_lease(last_heartbeat=None, started_at=None))
    # unknowable lease -> not expired -> busy (fail-closed), and says so
    assert status.state == STATE_BUSY
    assert "unknown" in status.reason


def test_classify_awaiting():
    status = _classify(awaiting_age_s=90.0)
    assert status.state == STATE_AWAITING
    assert status.is_defect is False


def test_classify_lost_goal():
    status = _classify(open_goal_text="ship the loop daemon")
    assert status.state == STATE_LOST_GOAL
    assert status.is_defect is True
    assert status.goal == "ship the loop daemon"


def test_classify_open_goal_without_auto_advance_is_idle():
    status = _classify(open_goal_text="ship the loop daemon", auto_advance=False)
    assert status.state == STATE_IDLE


def test_classify_idle():
    status = _classify()
    assert status.state == STATE_IDLE
    assert status.is_defect is False


# precedence
def test_busy_beats_everything():
    status = _classify(
        lease=_lease(),
        awaiting_age_s=10.0,
        open_goal_text="g",
        auto_advance=True,
    )
    assert status.state == STATE_BUSY


def test_stalled_beats_awaiting_and_goal():
    status = _classify(
        lease=_lease(last_heartbeat=NOW - 900),
        awaiting_age_s=10.0,
        open_goal_text="g",
    )
    assert status.state == STATE_STALLED


def test_awaiting_beats_lost_goal():
    """A thread blocked on a human is NOT a lost goal — that would be a false page."""
    status = _classify(awaiting_age_s=10.0, open_goal_text="g", auto_advance=True)
    assert status.state == STATE_AWAITING


def test_finished_track_does_not_count_as_busy():
    status = _classify(lease=_lease(status="completed"))
    assert status.state == STATE_IDLE


def test_stalled_only_applies_to_running_tracks():
    status = _classify(lease=_lease(status="completed", last_heartbeat=NOW - 9_000))
    assert status.state == STATE_IDLE


def test_empty_goal_text_is_not_a_goal():
    assert _classify(open_goal_text="   ").state == STATE_IDLE


def test_label_falls_back_to_lease_label():
    status = _classify(lease=_lease(label="Discord — #loop-daemon"), label="")
    assert status.label == "Discord — #loop-daemon"


# ── redis hash projection ────────────────────────────────────────────────────


def test_redis_hash_is_flat_strings():
    status = _classify(lease=_lease())
    payload = status.as_redis_hash()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in payload.items())
    assert payload["state"] == STATE_BUSY
    assert payload["age_s"] == "10"


def test_redis_hash_omits_unknown_age():
    payload = _classify(lease=_lease(last_heartbeat=None, started_at=None))
    assert "age_s" not in payload.as_redis_hash()


# ── summarize ────────────────────────────────────────────────────────────────


def test_summarize_counts_and_flags_defects():
    statuses = [
        SessionStatus("a", STATE_BUSY),
        SessionStatus("b", STATE_STALLED),
        SessionStatus("c", STATE_AWAITING),
        SessionStatus("d", STATE_LOST_GOAL),
        SessionStatus("e", STATE_IDLE),
        SessionStatus("f", STATE_IDLE),
    ]
    summary = summarize(statuses)
    assert summary["total"] == 6
    assert summary["counts"][STATE_IDLE] == 2
    assert summary["counts"][STATE_BUSY] == 1
    assert [s.session_id for s in summary["defects"]] == ["b", "d"]
    assert summary["healthy"] is False


def test_summarize_all_healthy():
    summary = summarize(
        [SessionStatus("a", STATE_BUSY), SessionStatus("b", STATE_IDLE)]
    )
    assert summary["healthy"] is True
    assert summary["defects"] == []


def test_summarize_empty():
    summary = summarize([])
    assert summary == {
        "total": 0,
        "counts": {state: 0 for state in summary["counts"]},
        "defects": [],
        "healthy": True,
    }


def test_defect_states_include_awaiting_stale_but_not_awaiting():
    # A fresh ``awaiting`` is the system working as designed and must stay
    # silent; ``awaiting_stale`` is a governor blocked on a decision and must
    # alert. Conflating them is the bug this split fixes.
    assert DEFECT_STATES == {STATE_STALLED, STATE_LOST_GOAL, STATE_AWAITING_STALE}
    assert STATE_AWAITING not in DEFECT_STATES
    assert STATE_BUSY not in DEFECT_STATES


def test_max_trusted_lease_is_sane():
    assert MAX_TRUSTED_LEASE_S >= DEFAULT_TRACK_LEASE_S
    assert math.isfinite(MAX_TRUSTED_LEASE_S)


# ── awaiting → awaiting_stale split ───────────────────────────────────────────
#
# Regression guard for the failure mode this split exists to fix: a thread parked
# awaiting a governor decision, whose prompt then ages out of the 1h freshness
# window, used to be reported as ``lost_goal`` / "nothing running". It was neither
# lost nor idle -- it was blocked on the governor, who was never told.


def test_stale_awaiting_is_not_reported_as_lost_goal():
    now = 1_000_000.0
    st = classify_session(
        "s1",
        lease=None,
        awaiting_age_s=None,  # aged out of the freshness window
        awaiting_any_age_s=90_000.0,  # 25h, but we still know it is parked
        open_goal_text="ship the nudge split",
        now=now,
        auto_advance=True,
    )
    assert st.state == STATE_AWAITING_STALE
    assert st.state != STATE_LOST_GOAL
    # The old reason ("nothing running") was the actively misleading part.
    assert "nothing running" not in st.reason.lower()
    assert "awaiting your decision" in st.reason.lower()


def test_fresh_awaiting_stays_reserved_and_silent():
    """A prompt that is still working must not become alertable."""
    now = 1_000_000.0
    st = classify_session(
        "s1",
        lease=None,
        awaiting_age_s=600.0,
        awaiting_any_age_s=600.0,
        open_goal_text="g",
        now=now,
        auto_advance=True,
    )
    assert st.state == STATE_AWAITING
    assert not st.is_defect


def test_stale_awaiting_without_goal_is_not_awaiting_stale():
    """No goal means no plan to be blocked on, so do not claim blocked-on-you.

    Without a goal the pre-existing fallthrough applies (``idle``); the
    point of the assertion is only that we do not manufacture an
    ``awaiting_stale`` alert out of a thread that has no plan at all.
    """
    st = classify_session(
        "s1",
        lease=None,
        awaiting_age_s=None,
        awaiting_any_age_s=90_000.0,
        open_goal_text=None,
        now=1_000_000.0,
        auto_advance=True,
    )
    assert st.state != STATE_AWAITING_STALE


def test_awaiting_stale_is_a_defect_so_it_actually_alerts():
    st = classify_session(
        "s1",
        lease=None,
        awaiting_age_s=None,
        awaiting_any_age_s=90_000.0,
        open_goal_text="g",
        now=1_000_000.0,
        auto_advance=True,
    )
    assert st.is_defect


def test_awaiting_any_age_ignores_the_freshness_cutoff():
    """``fresh_s=None`` must report the age instead of returning None."""
    now = 1_000_000.0
    # Matching is by suffix ":{ident}", so the key must be the transport
    # identity and the session id must end with it.
    sid = "pk-abc:tg:100:200"
    reg = {"tg:100:200": now - 90_000.0}
    capped = awaiting_age(sid, telegram_awaiting=reg, discord_awaiting={}, now=now)
    uncapped = awaiting_age(
        sid, telegram_awaiting=reg, discord_awaiting={}, now=now, fresh_s=None
    )
    assert capped is None
    assert uncapped == 90_000.0


# ── stale-awaiting suppression ────────────────────────────────────────────────
# An awaiting prompt whose registry entry is never removed ages forever and the
# loop keeps announcing a decision already made (seen at 77h). The guard must be
# positive-evidence-only: it may stop a false nag, never swallow a real question.


def _awaiting_kwargs(**over):
    kw = {
        "lease": None,
        "awaiting_age_s": None,
        "awaiting_any_age_s": 3600.0,
        "open_goal_text": "finish the thing",
        "now": NOW,
        "auto_advance": True,
    }
    kw.update(over)
    return kw


def test_stale_awaiting_kept_when_no_activity_evidence():
    """No activity timestamp -> unchanged behaviour, still awaiting."""
    st = classify_session("s1", **_awaiting_kwargs())
    assert st.state == STATE_AWAITING_STALE


def test_stale_awaiting_suppressed_when_session_acted_after_prompt():
    """Activity strictly newer than the prompt proves the governor replied."""
    prompt_ts = NOW - 3600.0
    st = classify_session("s1", **_awaiting_kwargs(last_activity_ts=prompt_ts + 60))
    assert st.state != STATE_AWAITING_STALE


def test_stale_awaiting_kept_when_activity_precedes_prompt():
    """Activity older than the prompt is not evidence of a reply."""
    prompt_ts = NOW - 3600.0
    st = classify_session("s1", **_awaiting_kwargs(last_activity_ts=prompt_ts - 60))
    assert st.state == STATE_AWAITING_STALE


def test_fresh_awaiting_suppressed_when_session_acted_after_prompt():
    prompt_ts = NOW - 120.0
    st = classify_session(
        "s1",
        **_awaiting_kwargs(awaiting_age_s=120.0, last_activity_ts=prompt_ts + 30),
    )
    assert st.state != STATE_AWAITING


def test_fresh_awaiting_kept_without_activity_evidence():
    st = classify_session("s1", **_awaiting_kwargs(awaiting_age_s=120.0))
    assert st.state == STATE_AWAITING


def test_suppression_never_swallows_a_goal():
    """A suppressed prompt must not leave the session looking done."""
    prompt_ts = NOW - 3600.0
    st = classify_session("s1", **_awaiting_kwargs(last_activity_ts=prompt_ts + 60))
    assert st.state != STATE_IDLE
    assert st.goal == "finish the thing"


def test_prompt_superseded_rejects_missing_and_garbage():
    assert _prompt_superseded(None, NOW, NOW) is False
    assert _prompt_superseded(3600.0, None, NOW) is False
    assert _prompt_superseded(3600.0, "not-a-number", NOW) is False
