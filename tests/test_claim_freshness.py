"""Claims must not read as activity when nothing has touched them.

Regression: the board showed 23 "open claims" alongside "0 workers in flight".
Both were true, but the pair reads as a contradiction. Nothing releases a claim
on crash or abandonment -- ``release()`` only fires on a clean turn end and only
for the claimer's own claims -- so rows survive indefinitely. The oldest was 44
days old. The board needs the age to say what the count means.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from app import github_client, live_state


def _utc_iso(seconds_ago: float) -> str:
    ts = time.time() - seconds_ago
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _serve(payload):
    """Patch the client the way live_state actually imports it."""

    class _C:
        def read_file(self, repo, path):
            return {"type": "file", "content": json.dumps(payload)}

    return _C


@pytest.fixture(autouse=True)
def _patched_client(monkeypatch):
    monkeypatch.setattr(github_client, "GitHubClient", _serve({"claims": []}))


def _set_claims(monkeypatch, claims):
    monkeypatch.setattr(github_client, "GitHubClient", _serve({"claims": claims}))


def test_fresh_claim_is_marked_fresh(monkeypatch):
    _set_claims(monkeypatch, [{"plan_file": "plans/A.md", "claimed_at": _utc_iso(60)}])
    claims, note = live_state._active_claims()
    assert note is None
    assert claims[0]["fresh"] is True
    assert 0 <= claims[0]["age_seconds"] <= 120


def test_stale_claim_is_flagged_and_aged(monkeypatch):
    """The 44-day row must not look like active supervision."""
    _set_claims(
        monkeypatch,
        [
            {
                "plan_file": "plans/Daemon.md",
                "supervisor": "Envoy",
                "claimed_at": _utc_iso(44 * 86400),
            }
        ],
    )
    claims, _ = live_state._active_claims()
    assert claims[0]["fresh"] is False
    assert claims[0]["age_seconds"] > 43 * 86400


def test_unparseable_timestamp_is_not_called_fresh(monkeypatch):
    """Fail closed: unknown age must never be presented as current."""
    _set_claims(monkeypatch, [{"plan_file": "plans/B.md", "claimed_at": "not-a-date"}])
    claims, _ = live_state._active_claims()
    assert claims[0]["fresh"] is False
    assert claims[0]["age_seconds"] is None


def test_payload_splits_fresh_and_stale_counts(monkeypatch):
    _set_claims(
        monkeypatch,
        [
            {"plan_file": "plans/A.md", "supervisor": "x", "claimed_at": _utc_iso(60)},
            {
                "plan_file": "plans/B.md",
                "supervisor": "y",
                "claimed_at": _utc_iso(10 * 3600),
            },
        ],
    )
    monkeypatch.setattr(live_state, "_sessions", lambda: ([], None))
    monkeypatch.setattr(live_state, "_workers", lambda: ({}, None))
    monkeypatch.setenv("LIVE_STATE_PUBLIC", "1")
    out = live_state.live_state_payload()
    assert out["claim_count"] == 2
    assert out["claim_count_fresh"] == 1
    assert out["claim_count_stale"] == 1


def test_age_accepts_zulu_and_offset_forms():
    """Both spellings of UTC must give the same age."""
    now = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    for raw in ("2026-10-30T07:00:00Z", "2026-10-30T07:00:00+00:00"):
        age = live_state._claim_age_seconds(raw, now=now)
        assert age is not None and abs(age - 3600) < 1, f"{raw} -> {age}"


def test_naive_timestamp_is_read_as_utc():
    """A timestamp with no tzinfo must not shift by the host's local offset."""
    now = datetime(2026, 10, 30, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    age = live_state._claim_age_seconds("2026-10-30T07:00:00", now=now)
    assert age is not None and abs(age - 3600) < 1, age
