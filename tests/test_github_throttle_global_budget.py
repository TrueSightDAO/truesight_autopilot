"""PR-C -- the GitHub concurrency governor must bound the FLEET, not a process.

Plan: ``plans/SOPHIA_DISPATCH_RECONCILIATION.md`` unit PR-C (formerly
``SOPHIA_MULTI_PROCESS_WORKER_PLAN.md`` PR6).

The regression these tests exist for: ``github_throttle`` built its semaphore at
import time, so once ``uvicorn --workers 4`` went live the fleet permitted
**4 x 4 = 16** concurrent GitHub calls against one 5,000/h PAT. No single
process could observe that -- every process's own semaphore looked correct. Only
a crossing-processes test can catch it, so the invariant test here runs real
subprocesses against the real local Redis.

Conventions (same as ``tests/test_redis_claim.py``): everything talks to the
live local Redis on 127.0.0.1:6379, because mocking a distributed budget tests
the mock rather than Redis's actual Lua/INCR semantics. CI has no Redis, so the
module skips there with the reason printed.

Every test uses a UNIQUE budget key (and conftest pins a test namespace for the
whole suite), so running the tests can never perturb the production counter.

Locally, export ``PHASE_C_REDIS_PASSWORD`` to exercise these (the suite redirects
``VAULT_DIR`` to a temp dir, so the vault lookup that production uses finds
nothing there) -- otherwise the module skips rather than pretending to pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

os.environ.setdefault("AUTOPILOT_CHAT_URL", "http://localhost:8001")

try:
    from app import github_throttle as gt
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"github_throttle import unavailable: {exc}", allow_module_level=True)


def _client_or_skip():
    """Live local Redis client, or skip the module with the real reason.

    Clears the module's post-failure cooldown first. A test that deliberately
    simulates a Redis outage leaves that cooldown armed (correctly -- see the
    module docstring), and without this reset every later test in the file would
    SKIP instead of running. That is how the cross-process invariant test first
    appeared to pass while never actually executing.
    """
    gt._client = None
    gt._redis_retry_at = 0.0
    try:
        client = gt._get_client()
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        gt._client = None
        gt._redis_retry_at = 0.0
        pytest.skip(f"live Redis required for the budget invariant tests: {exc}")


@pytest.fixture()
def budget(monkeypatch):
    """A private budget key + a small fleet limit, isolated per test."""
    key = f"test:gh:budget:{uuid.uuid4().hex}"
    monkeypatch.setenv("GITHUB_THROTTLE_BUDGET_KEY", key)
    monkeypatch.setenv("GITHUB_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("GITHUB_THROTTLE_LEASE_MS", "60000")
    monkeypatch.setenv("GITHUB_THROTTLE_TIMEOUT_SECS", "5")
    client = _client_or_skip()
    client.delete(key)
    yield key
    client.delete(key)


def test_acquire_and_release_move_the_shared_counter(budget):
    """One acquire is visible fleet-wide; the release puts it back."""
    client = _client_or_skip()
    assert client.get(budget) in (None, "0")

    assert gt._acquire_global(1.0) is True
    assert client.get(budget) == "1"
    assert gt.global_inflight() == 1
    assert gt.remaining_globally() == gt.max_concurrency() - 1

    gt._release_global()
    assert client.get(budget) in (None, "0")
    assert gt.global_inflight() == 0


def test_counter_never_exceeds_the_fleet_limit(budget):
    """The atomic increment-if-below-limit refuses once the limit is reached."""
    client = _client_or_skip()
    limit = gt.max_concurrency()
    for _ in range(limit):
        assert gt._acquire_global(1.0) is True
    assert client.get(budget) == str(limit)

    # The next attempt must lose -- this is the overrun the old per-process
    # semaphore could not see. A short timeout keeps the test fast.
    assert gt._acquire_global(0.05) is False
    assert client.get(budget) == str(limit)

    for _ in range(limit):
        gt._release_global()
    assert client.get(budget) in (None, "0")


def test_release_floors_at_zero(budget):
    """A stray extra release must not hand the fleet a free extra permit."""
    client = _client_or_skip()
    gt._release_global()
    gt._release_global()
    assert client.get(budget) in (None, "0")
    assert gt.remaining_globally() == gt.max_concurrency()


def test_budget_self_heals_after_the_lease_expires(budget, monkeypatch):
    """A crashed holder's leaked token must not wedge the fleet forever."""
    client = _client_or_skip()
    limit = gt.max_concurrency()
    # Simulate N workers that died holding permits: counter at the limit,
    # short lease, no releaser.
    client.set(budget, str(limit), px=250)
    monkeypatch.setenv("GITHUB_THROTTLE_LEASE_MS", "250")

    assert gt._acquire_global(0.05) is False, "leaked tokens should block first"

    time.sleep(0.4)  # let the lease expire
    assert gt._acquire_global(1.0) is True, "budget did not self-heal after lease"
    gt._release_global()


def test_fail_open_when_redis_is_unreachable(monkeypatch):
    """Redis down must degrade to the per-process bound, never raise or hang."""

    def boom():
        raise RuntimeError("simulated Redis outage")

    monkeypatch.setattr(gt, "_get_client", boom)
    monkeypatch.setenv("GITHUB_THROTTLE_TIMEOUT_SECS", "1")

    with gt.github_throttle():
        pass  # reaching here at all is the assertion

    # The failure must also arm the retry cooldown, so one outage cannot add a
    # socket-connect stall to every remaining GitHub call in the turn.
    assert gt._redis_retry_at > time.monotonic(), "retry cooldown was not armed"

    gt._redis_retry_at = 0.0  # leave the module clean for subsequent tests
    assert gt.global_inflight() is None or gt.global_inflight() >= 0


def test_github_throttle_context_releases_the_global_permit(budget):
    """The context manager is balanced: no token leaks on the happy path."""
    client = _client_or_skip()
    with gt.github_throttle():
        assert client.get(budget) == "1"
    assert client.get(budget) in (None, "0")

    # and it is balanced on an exception too
    with pytest.raises(RuntimeError):
        with gt.github_throttle():
            raise RuntimeError("boom")
    assert client.get(budget) in (None, "0")


def test_local_permit_view_is_unchanged():
    """``remaining_permits()`` keeps its per-process meaning (compat)."""
    before = gt.remaining_permits()
    with gt.github_throttle():
        assert gt.remaining_permits() == before - 1
    assert gt.remaining_permits() == before


# ── the regression test: the bound holds ACROSS processes ────────────────────
_CHILD = """
import os, sys, time
sys.path.insert(0, os.environ["PRC_ROOT"])
from app import github_throttle as gt
with gt.github_throttle():
    started = time.time()
    time.sleep(float(os.environ["HOLD_S"]))
released = time.time()
with open(os.environ["OUT_FILE"], "a") as f:
    f.write("%f %f\\n" % (started, released))
"""


def _max_overlap(intervals):
    """Peak number of simultaneously-open intervals."""
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], e[1]))  # ends before starts at equal t
    peak = live = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    return peak


def test_the_bound_holds_across_processes(budget, tmp_path):
    """THE regression test: N real processes must never exceed the fleet limit.

    Six subprocesses each enter ``github_throttle()`` and hold for 0.4s, against
    a limit of 2. Before this fix each process had its own semaphore of 2, so up
    to 12 would have overlapped; the assertion is that at most 2 do.
    """
    _client_or_skip()
    root = os.path.dirname(os.path.dirname(gt.__file__))
    out = tmp_path / "spans.txt"
    env = dict(os.environ)
    env.update(
        {
            "PRC_ROOT": root,
            "OUT_FILE": str(out),
            "HOLD_S": "0.4",
            "GITHUB_THROTTLE_BUDGET_KEY": budget,
            "GITHUB_MAX_CONCURRENCY": "2",
            "GITHUB_THROTTLE_TIMEOUT_SECS": "20",
        }
    )

    procs = [
        subprocess.Popen([sys.executable, "-c", _CHILD], env=env) for _ in range(6)
    ]
    for p in procs:
        assert p.wait(timeout=90) == 0, "a throttle child process failed"

    intervals = []
    for line in out.read_text().splitlines():
        if line.strip():
            start, end = line.split()
            intervals.append((float(start), float(end)))

    assert len(intervals) == 6, f"expected 6 spans, got {intervals}"
    peak = _max_overlap(intervals)
    assert peak <= 2, (
        f"{peak} processes held a GitHub permit at once but the fleet limit "
        f"is 2 -- the bound is still per-process"
    )
    assert peak >= 2, f"never ran 2 concurrently (peak={peak}); test is too weak"
