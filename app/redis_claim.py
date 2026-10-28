"""Redis-backed mark-and-claim dispatch primitives (mark is not a claim).

Two different operations with two different owners:

* **mark** -- "this thread has unprocessed work". Idempotent, written by an
  adapter on every inbound message. Any idle worker may discover it.
* **claim** -- "I am the worker acting on this thread right now". An atomic
  distributed lock (``SET lock:<thread_id> <worker_id> NX EX <ttl>``) providing
  mutual exclusion, so two workers can never act on one thread concurrently.

The distinction is the whole point: a mark is an invitation, a claim is exclusive
ownership. A worker must ``try_claim`` and win before doing any work.

HONEST LIMITATION (plan section 0): Sophia runs as exactly ONE process today, so
none of this changes observable behaviour yet -- there is no second worker for it
to coordinate with. This module is tested in isolation against the real local
Redis (not mocked). Multi-process deployment is separate, not-yet-scoped work.
Do not read the presence of this module as "multi-instance Sophia is live".

Credentials: the Redis password lives in the vault as ``PHASE_C_REDIS_PASSWORD``
(the same retrieval pattern as ``DISCORD_BOT_TOKEN``), never in env or logs.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# --- Key naming -------------------------------------------------------------
LOCK_KEY_PREFIX = "lock:"
MARKS_KEY = "marks"

# A claim TTL must outlive the longest plausible single turn, because the TTL is
# what stops a dead worker from holding a thread forever. The sibling plan
# (SOPHIA_LOCK_EXPIRY_AND_LOAD_TEST_PLAN.md section 1) measured real turn
# durations on this box: p50 ~60s, p90 ~220s, max 1295s (21.6 min). 3600s (1h)
# is comfortably beyond that observed max while still bounding a dead worker's
# blast radius to an hour rather than forever.
DEFAULT_CLAIM_TTL_SECS = 3600

# Check-owner-then-delete. A plain DEL would let a worker whose own TTL already
# expired delete a lock a DIFFERENT worker has since acquired -- breaking mutual
# exclusion exactly when it matters. Comparing the stored value to our worker_id
# inside Redis makes the check-and-delete atomic.
_RELEASE_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


def _redis_url() -> str:
    return os.environ.get("SOPHIA_REDIS_URL", "redis://127.0.0.1:6379/0")


def _redis_password() -> str | None:
    """Redis password: vault first (canonical), then env as an explicit override."""
    env_pw = os.environ.get("PHASE_C_REDIS_PASSWORD")
    if env_pw:
        return env_pw
    from .vault import Vault  # local import: keep module import cheap

    v = Vault()
    v.initialize()
    return v.get_value("PHASE_C_REDIS_PASSWORD")


def get_client() -> Any:
    """Build a Redis client for the local instance (decode_responses for text)."""
    import redis  # imported lazily so `import app.redis_claim` never needs the dep

    return redis.Redis.from_url(
        _redis_url(),
        password=_redis_password(),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def lock_key(thread_id: str | int) -> str:
    return f"{LOCK_KEY_PREFIX}{thread_id}"


# --- mark -------------------------------------------------------------------
def mark(thread_id: str | int, client: Any = None, *, now: float | None = None) -> None:
    """Record that *thread_id* has unprocessed work. Idempotent.

    A sorted set keyed by timestamp is used rather than ``SADD``: it gives the
    same idempotency (re-marking a thread overwrites its score instead of adding
    a duplicate member) but additionally preserves *when* the mark was last
    refreshed, which PR5's cleanup needs -- unclaimed marks must expire on a
    time horizon, and a plain set carries no age to expire against. It also lets
    a future discovery loop pop oldest-first via ``ZPOPMIN`` instead of scanning
    the whole set.

    Re-marking an already-marked thread refreshes its timestamp (latest inbound
    wins), so a steady stream of messages cannot look "old" and get pruned.
    """
    import time as _time

    cli = client or get_client()
    ts = _time.time() if now is None else now
    cli.zadd(MARKS_KEY, {str(thread_id): ts})


def is_marked(thread_id: str | int, client: Any = None) -> bool:
    """Whether *thread_id* currently carries a mark. Read-only helper."""
    cli = client or get_client()
    return cli.zscore(MARKS_KEY, str(thread_id)) is not None


def clear_mark(thread_id: str | int, client: Any = None) -> bool:
    """Remove *thread_id*'s mark (a worker does this once it has claimed it)."""
    cli = client or get_client()
    return bool(cli.zrem(MARKS_KEY, str(thread_id)))


# --- claim ------------------------------------------------------------------
def try_claim(
    thread_id: str | int,
    worker_id: str,
    ttl: int = DEFAULT_CLAIM_TTL_SECS,
    client: Any = None,
) -> bool:
    """Atomically claim *thread_id* for *worker_id*. True iff we won.

    ``SET NX EX`` is atomic in Redis, so exactly one concurrent caller can win a
    free lock. Fails (returns False) if another worker holds it -- including if
    *worker_id* already holds it, since re-entrant claiming is NOT provided. A
    worker that re-claims its own live thread would have no way to distinguish
    "I still own this" from "a peer does", so callers should treat a held lock as
    "somebody is on it" regardless of whose id is stored.
    """
    if not worker_id:
        raise ValueError("worker_id is required to claim a thread")
    if ttl <= 0:
        raise ValueError(f"ttl must be > 0 (got {ttl!r})")

    cli = client or get_client()
    return bool(cli.set(lock_key(thread_id), worker_id, nx=True, ex=int(ttl)))


def release(thread_id: str | int, worker_id: str, client: Any = None) -> bool:
    """Release *thread_id* ONLY if *worker_id* currently owns it. True iff deleted.

    Uses a Lua ``EVAL`` for an atomic check-owner-then-delete (never a plain
    ``DEL``). If our TTL already lapsed and a different worker has since claimed
    the thread, this returns False and leaves that worker's lock intact.
    """
    if not worker_id:
        raise ValueError("worker_id is required to release a thread")

    cli = client or get_client()
    result = cli.eval(_RELEASE_LUA, 1, lock_key(thread_id), worker_id)
    return bool(result)


def owner_of(thread_id: str | int, client: Any = None) -> str | None:
    """Current lock owner's worker_id, or None if unclaimed. Read-only helper."""
    cli = client or get_client()
    return cli.get(lock_key(thread_id))


def claim_ttl_remaining(thread_id: str | int, client: Any = None) -> int:
    """Seconds left on *thread_id*'s claim; -2 unclaimed, -1 no expiry."""
    cli = client or get_client()
    return int(cli.ttl(lock_key(thread_id)))
