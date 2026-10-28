"""PR1 -- global GitHub concurrency governor + thread-safe read cache.

Plan: SOPHIA_GITHUB_RATE_LIMIT_GOVERNOR_PLAN.md, PR1. The plan's UAT section asks
for exactly two things, both covered here:

  1. "a unit test asserting the semaphore is held during a mocked GitHub read"
  2. "_read_cache stays consistent under concurrent access"

No network: the GitHub read is mocked, so these run on CI without a PAT.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

from app import deploy_ledger, github_throttle
from app.tools import github_tools


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts and ends with an empty read cache and a full permit pool."""
    github_tools._read_cache.clear()
    yield
    github_tools._read_cache.clear()


# ── the governor itself ──────────────────────────────────────────────────────
def test_default_permit_count_matches_plan():
    """Plan specifies default 4."""
    assert github_throttle.DEFAULT_MAX_CONCURRENCY == 4


def test_semaphore_is_shared_across_both_guarded_modules():
    """The whole point: ONE global bound, not one per module.

    A per-module semaphore would satisfy the plan's wording while letting the two
    hottest paths (reads and deploy-ledger) permit 2x the intended concurrency.
    """
    assert github_tools.github_throttle is github_throttle.github_throttle
    assert deploy_ledger.github_throttle is github_throttle.github_throttle


def test_throttle_bounds_actual_concurrency():
    """8 threads racing through the throttle never exceed max_concurrency."""
    max_c = github_throttle.max_concurrency()
    live = 0
    peak = 0
    lock = threading.Lock()

    def worker():
        nonlocal live, peak
        with github_throttle.github_throttle():
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.03)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(max_c * 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak <= max_c, f"peak concurrency {peak} exceeded bound {max_c}"
    assert github_throttle.remaining_permits() == max_c, "permits leaked"


def test_permits_are_released_when_the_body_raises():
    """A permit must not leak if the guarded call throws (e.g. a 403)."""
    before = github_throttle.remaining_permits()
    with pytest.raises(RuntimeError), github_throttle.github_throttle():
        raise RuntimeError("boom")
    assert github_throttle.remaining_permits() == before


def test_garbage_env_falls_back_instead_of_disabling_the_governor(monkeypatch):
    """A typo must never set the permit count to 0 and block every call forever."""
    for bad in ("not-a-number", "0", "-3", ""):
        monkeypatch.setenv("GITHUB_MAX_CONCURRENCY", bad)
        assert github_throttle.max_concurrency() == 4
    monkeypatch.setenv("GITHUB_MAX_CONCURRENCY", "7")
    assert github_throttle.max_concurrency() == 7


# ── REQUIRED #1: semaphore is held during a mocked GitHub read ───────────────
def test_semaphore_is_held_during_a_mocked_github_read(monkeypatch):
    """Plan UAT requirement 1.

    Proven positively rather than by timing: the mocked httpx.get itself inspects
    the permit pool *from inside the call*. If the read were unguarded, all 4
    permits would still be free at that instant.
    """
    max_c = github_throttle.max_concurrency()
    seen_free_inside_call = {}

    class FakeResp:
        status_code = 200
        url = "https://api.github.com/fake"
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": "aGVsbG8=",
                "encoding": "base64",
                "size": 5,
                "html_url": "https://x",
            }

    def fake_get(*a, **kw):
        # INSPECT from inside the guarded region.
        seen_free_inside_call["permits"] = github_throttle.remaining_permits()
        return FakeResp()

    monkeypatch.setattr(github_tools.httpx, "get", fake_get)

    out = github_tools.read_repo_file("agentic_ai_context", "SOME_FILE.md", ref="main")
    assert out["type"] == "file", out

    assert seen_free_inside_call["permits"] == max_c - 1, (
        "semaphore was NOT held during the GitHub read: all "
        f"{max_c} permits were free inside the call"
    )
    # and released on exit
    assert github_throttle.remaining_permits() == max_c


def test_semaphore_is_held_during_a_mocked_code_search(monkeypatch):
    """Same guarantee on the other guarded read path."""
    max_c = github_throttle.max_concurrency()
    seen = {}

    class FakeResp:
        status_code = 200
        url = "https://api.github.com/search/code"
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"total_count": 0, "items": []}

    def fake_get(url, **kw):
        seen["permits"] = github_throttle.remaining_permits()
        return FakeResp()

    monkeypatch.setattr(github_tools.httpx, "get", fake_get)
    out = github_tools.search_codebase("agentic_ai_context", "foo")
    assert out["type"] == "search_results"
    assert seen["permits"] == max_c - 1
    assert github_throttle.remaining_permits() == max_c


def test_deploy_ledger_write_holds_one_permit_across_both_calls(monkeypatch):
    """_put_file does a sha GET then a PUT -- it must hold ONE permit for both.

    Releasing between them would let a sibling write land in the gap and stale
    the sha we computed, producing a lost update.
    """
    max_c = github_throttle.max_concurrency()
    observed = []

    class FakeResp:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "sha": "abc123",
                "content": {"html_url": "u"},
                "commit": {"sha": "c"},
            }

    def fake_get(*a, **kw):
        observed.append(("get", github_throttle.remaining_permits()))
        return FakeResp()

    def fake_put(*a, **kw):
        observed.append(("put", github_throttle.remaining_permits()))
        return FakeResp()

    monkeypatch.setattr(deploy_ledger.httpx, "get", fake_get)
    monkeypatch.setattr(deploy_ledger.httpx, "put", fake_put)

    out = deploy_ledger._put_file("deploys/x.json", "{}", "msg")
    assert out.get("status") == "success", out

    assert len(observed) == 2
    for label, free in observed:
        assert free == max_c - 1, f"{label} was not under a permit (free={free})"
    assert github_throttle.remaining_permits() == max_c


# ── REQUIRED #2: _read_cache consistency under concurrent access ─────────────
def test_read_cache_stays_consistent_under_concurrent_access():
    """Plan UAT requirement 2.

    Hammers _cache_get/_cache_set from many threads, including sizes well past
    _GITHUB_READ_CACHE_MAX so the eviction path (len >= MAX -> clear) runs
    concurrently. An unguarded dict here can raise during get-then-pop or lose
    entries mid-clear; the assertion is that no exception escapes and every
    surviving entry is a well-formed (float_ts, dict) tuple.
    """
    cache_max = github_tools._GITHUB_READ_CACHE_MAX
    errors = []
    barrier = threading.Barrier(8)

    def worker(n):
        try:
            barrier.wait(timeout=10)
            for i in range(cache_max // 4):
                key = ("read", f"repo{n}", f"path{i % 50}", "main")
                github_tools._cache_set(key, {"type": "file", "content": str(i)})
                got = github_tools._cache_get(key)
                if got is not None and not isinstance(got, dict):
                    errors.append(f"corrupt value: {got!r}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent cache access raised: {errors[:3]}"
    for key, val in github_tools._read_cache.items():
        assert isinstance(key, tuple) and len(key) == 4, key
        assert isinstance(val, tuple) and len(val) == 2, val
        ts, payload = val
        assert isinstance(ts, float) and isinstance(payload, dict), val


def test_cache_eviction_never_exceeds_max_size():
    """The bound holds: a concurrent writer cannot push it past _GITHUB_READ_CACHE_MAX."""
    cache_max = github_tools._GITHUB_READ_CACHE_MAX
    for i in range(cache_max + 250):
        github_tools._cache_set(("read", "r", f"p{i}", "main"), {"i": i})
        assert len(github_tools._read_cache) <= cache_max


def test_cache_hit_avoids_the_github_call(monkeypatch):
    """Caching still works after the lock was added (no behaviour regression)."""
    calls = []

    class FakeResp:
        status_code = 200
        url = "https://api.github.com/fake"
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": "aGVsbG8=",
                "encoding": "base64",
                "size": 5,
                "html_url": "https://x",
            }

    def fake_get(*a, **kw):
        calls.append(1)
        return FakeResp()

    monkeypatch.setattr(github_tools.httpx, "get", fake_get)
    first = github_tools.read_repo_file("agentic_ai_context", "X.md")
    second = github_tools.read_repo_file("agentic_ai_context", "X.md")
    assert first == second
    assert len(calls) == 1, "second read should have been served from cache"
