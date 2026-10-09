"""Session context compaction — PR0 of SOPHIA_CONTEXT_COMPACTION_PLAN.

Why this exists
---------------
Long-running Telegram threads grow without bound: every turn appends user +
assistant-with-tool_calls + tool-result messages and nothing ever shrinks the
history. Real sessions reach 38,000-50,000 tokens per LLM call, which slows
every round, keeps the single worker's event loop busy longer, and makes
/health probes occasionally miss their window. This module folds *old* history
into a single compact summary message (reusing each old turn's own
"\u2705 Done this turn" report text where present) so the live working copy handed
to the LLM stays small.

Scope (PR0)
-----------
This is the compaction *library* only. It is deliberately NOT wired into the
live turn path (that is PR2, after a manual trigger in PR1 has been validated
against real bloated sessions). Importing or calling these functions changes
nothing about any running session — they only read ``messages`` and return new
lists / extracted text.

Design invariants (from the plan, section 2)
--------------------------------------------
1. Compaction never changes what's provably true — the on-disk session JSON is
   never touched here (PR1/PR2 take a ``<hash>.pre-compact-*.json`` backup
   before any rewrite); the GitHub transcript repo remains the complete audit
   record.
2. Boundaries always land on a FULL TURN boundary — never inside a
   ``tool_calls`` -> ``tool`` sequence (DeepSeek 400s otherwise). A "turn" is
   one ``user`` message through the final assistant reply with no pending
   tool calls.
3. The most recent ``keep_last_n_turns`` turns always stay VERBATIM,
   uncompacted — recent context is where the model needs full fidelity.
4. Everything older folds into ONE synthetic summary message (role ``user``,
   content prefixed ``[CONTEXT SUMMARY \u2014 turns 1\u2013K compacted ...]``),
   reusing each compacted turn's "\u2705 Done this turn" report text where
   present; no LLM call is made by this module.
5. Token-count gate: no-op (returns a content-identical copy) when the history
   is at or under ``token_threshold``.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# The exact marker every completed turn ends with (see _build_turn_report in
# app/main.py): a "**\u2705 Done this turn \u2014 actions taken:**" heading followed by
# bullet lines. Reused verbatim as the per-turn summary text.
DONE_THIS_TURN_RE = re.compile(
    r"\*\*\s*\u2705\s*Done this turn\s*[:\u2014-]\s*actions taken\s*:\s*\*\*(.*?)(?=\n\s*\n|\Z)",
    re.DOTALL,
)

# Default retained-tail / trigger knobs (invariant 4 + 6 of the plan).
DEFAULT_KEEP_LAST_TURNS = 6
DEFAULT_TOKEN_THRESHOLD = 20_000

# Prefix of the synthetic summary message (role `user`, matching the
# GOVERNOR_IDENTITY precedent of a bracketed-context user message).
SUMMARY_PREFIX = "[CONTEXT SUMMARY \u2014 turns 1\u2013{k} compacted, full history in transcript repo]:"


def find_turn_boundaries(messages: list[dict]) -> list[int]:
    """Indices of every COMPLETED turn end in ``messages`` (ascending).

    A turn = one ``user`` message through the final assistant reply that has no
    pending tool calls. A message at index ``i`` is a completed turn end iff:

      * it is a plain assistant message (no ``tool_calls``), AND
      * the next message is a ``user`` message, OR it is the final message.

    Anything else — assistant messages that still carry ``tool_calls`` (their
    ``tool`` results follow), ``tool`` results themselves, and trailing in-flight
    fragments of an unfinished turn — is never a boundary. Callers can therefore
    always cut at ``boundary + 1`` without splitting a ``tool_calls``/``tool``
    pair.
    """
    n = len(messages)
    ends: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") != "assistant" or m.get("tool_calls"):
            continue  # not a completed assistant reply
        j = i + 1
        if j >= n or messages[j].get("role") == "user":
            ends.append(i)
    return ends


def extract_done_this_turn(messages: list[dict], start: int, end: int) -> str | None:
    """Extract the "\u2705 Done this turn" report from the turn spanning [start, end).

    The report lives at the END of the turn's final plain-assistant message
    (appended by ``_build_turn_report`` in app/main.py). Returns the matched
    block text (everything after the heading marker, stripped) or ``None`` if
    the span has no usable marker.
    """
    if start >= end:
        return None
    last_plain: dict | None = None
    for m in messages[start:end]:
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            last_plain = m
    if last_plain is None:
        return None
    content = last_plain.get("content") or ""
    if not isinstance(content, str):
        return None
    match = DONE_THIS_TURN_RE.search(content)
    if not match:
        return None
    block = match.group(1).strip()
    return block or None


def count_tokens(messages: list[dict], model: str | None = None) -> int:
    """Token count for a message list via ``litellm.token_counter``.

    Falls back to the codebase's observed dense 2-chars/token ratio if litellm
    is unavailable or errors (same fallback as ``_history_token_count`` in
    app/main.py).
    """
    if not messages:
        return 0
    try:
        import litellm

        return int(
            litellm.token_counter(
                model=model or "deepseek/deepseek-flash", messages=messages
            )
        )
    except Exception:  # noqa: BLE001 — offline/absent litellm; cheap fallback
        return sum(len(str(m.get("content", "") or "")) for m in messages) // 2


def default_summarizer(messages: list[dict], start: int, end: int) -> str:
    """Summary text for the compacted region ``[start, end)``.

    Concatenates each completed turn's "\u2705 Done this turn" block (the free
    per-turn summary the codebase already produces), one per turn. Turns that
    lack the marker (or the region's leading orphan fragments left by an older
    edge-trim) are skipped — they add no summary line. When NO turn in the
    region has a marker, returns a short structural note so the model still
    knows work happened.
    """
    parts: list[str] = []
    i = start
    while i < end:
        if messages[i].get("role") != "user":
            i += 1  # system tags / orphan fragments from an old edge-trim
            continue
        j = i + 1
        while j < end and messages[j].get("role") != "user":
            j += 1
        block = extract_done_this_turn(messages, i, j)
        if block:
            opener = str(messages[i].get("content") or "")[:120].strip()
            parts.append(f"Turn \u2014 {opener}:\n{block}" if opener else block)
        i = j
    if parts:
        return "\n\n".join(parts)
    user_turns = sum(1 for m in messages[start:end] if m.get("role") == "user")
    tool_calls = sum(len(m.get("tool_calls") or []) for m in messages[start:end])
    return (
        f"{user_turns} earlier turn(s) in this thread were compacted; "
        f"{tool_calls} tool call(s) executed across them. Full detail is in the "
        "session transcript repo."
    )


def compact_history(
    messages: list[dict],
    keep_last_n_turns: int = DEFAULT_KEEP_LAST_TURNS,
    token_threshold: int = DEFAULT_TOKEN_THRESHOLD,
    model: str | None = None,
    summarizer: Callable[[list[dict], int, int], str] | None = None,
) -> list[dict]:
    """Return a compacted COPY of ``messages`` (never mutates the input).

    No-op (a fresh, content-identical list) when the history is at or under
    ``token_threshold`` tokens, when ``token_threshold <= 0`` (disabled), or
    when there is no completed turn old enough to fold (at most
    ``keep_last_n_turns`` completed turns in total).

    When compaction DOES happen, the result is::

        [leading system messages] + [ONE synthetic user summary] + [retained tail]

    where the retained tail is the ``keep_last_n_turns`` most recent COMPLETED
    turns plus any trailing in-flight (unfinished) messages — preserved
    verbatim, byte-identical to the input — and the summary is
    ``summarizer(messages, head_len, tail_start)`` (defaults to
    ``default_summarizer``, the "Done this turn" text-reuse path; no LLM call).
    """
    if not messages:
        return []
    if token_threshold <= 0:
        return list(messages)
    if count_tokens(messages, model=model) <= token_threshold:
        return list(messages)

    turn_ends = find_turn_boundaries(messages)
    if len(turn_ends) <= keep_last_n_turns:
        # Nothing old enough to fold while still keeping K turns verbatim.
        return list(messages)

    # Retained tail starts right after the (K+1)-th most recent completed turn
    # end — i.e. keep the last K turn ends verbatim, fold everything before the
    # first of those K. (turn_ends[-keep_last_n_turns] would be the FIRST kept
    # end and would wrongly cut into it.)
    tail_start = turn_ends[-keep_last_n_turns - 1] + 1

    # System messages (role tag, [PINNED] working-set notes) are never
    # compacted or dropped — the same guarantee the token-trim gives. Leading
    # ones stay at the front; any mid-list system message (rare) is kept after
    # them, before the summary.
    head_sys: list[dict] = []
    k = 0
    while k < tail_start and messages[k].get("role") == "system":
        head_sys.append(messages[k])
        k += 1
    extra_sys = [m for m in messages[k:tail_start] if m.get("role") == "system"]
    sum_start = k  # first non-system message of the compacted region
    if sum_start >= tail_start:
        return list(messages)  # pathological — only system msgs before the tail

    summ = (summarizer or default_summarizer)(messages, sum_start, tail_start)
    if not summ.strip():
        return head_sys + extra_sys + messages[tail_start:]
    compacted_turns = len([b for b in turn_ends if b < tail_start])
    summary_msg = {
        "role": "user",
        "content": SUMMARY_PREFIX.format(k=compacted_turns) + "\n" + summ,
    }
    return head_sys + extra_sys + [summary_msg] + messages[tail_start:]


def backup_session_file(session_path: str | Path) -> Path | None:
    """Copy an on-disk session JSON to ``<name>.pre-compact-<UTC ts>.json``.

    Returns the backup Path on success, or ``None`` when the source file does
    not exist (nothing to back up). PR1/PR2 call this BEFORE any rewrite of a
    session file so a bad compaction is trivially reversible. This function
    only copies — it never modifies the source.
    """
    src = Path(session_path)
    if not src.is_file():
        return None
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = src.with_name(f"{src.name}.pre-compact-{ts}.json")
    try:
        shutil.copy2(src, backup)
        logger.info("Backed up session file %s -> %s", src.name, backup.name)
        return backup
    except OSError as exc:  # pragma: no cover — fs errors are env-specific
        logger.warning("Session backup failed for %s: %s", src.name, exc)
        return None


# ---------------------------------------------------------------------------
# In-turn (intra-turn) compaction — Unit 1 of SOPHIA_INTRATURN_COMPACTION_PLAN
# ---------------------------------------------------------------------------
#
# The functions above fold COMPLETED prior turns into one summary. This section
# adds the complementary piece: folding the EARLY rounds of a single turn that
# is still in progress, so an investigation-heavy turn that needs many tool
# rounds (each dumping a full ssh_run stdout / file read) stops re-sending every
# raw round payload on every subsequent round.
#
# Contract mirrors the inter-turn machinery — same keep-verbatim / no-LLM / tool
# protocol conventions — just applied at ROUND granularity instead of turn
# granularity. A "round" is one assistant message carrying ``tool_calls`` plus
# the ``tool`` result messages that answer every id in it.

# Knobs (plan section 2.2): keep the last K rounds raw, fold earlier ones once
# the turn's own round count / token contribution crosses a threshold.
DEFAULT_KEEP_LAST_K_ROUNDS = 4
DEFAULT_INTRATURN_TOKEN_THRESHOLD = 40_000
DEFAULT_INTRATURN_ROUND_THRESHOLD = 8

# Header of the synthetic mid-turn summary message (role ``user``, same
# bracketed-context precedent as the inter-turn SUMMARY_PREFIX and [PINNED]).
TURN_IN_PROGRESS_PREFIX = (
    "[TURN IN PROGRESS \u2014 EARLIER ROUNDS SUMMARY \u2014 {k} earlier round(s) "
    "folded to a summary, raw output of the last {m} round(s) kept below]:"
)

# Per-snippet cap so one verbose ssh_run dump can't dominate the summary line.
ROUND_SNIPPET_CHARS = 200


def find_open_turn_start(messages: list[dict]) -> int | None:
    """Index of the ``user`` message that opens the LAST (current) turn.

    The intra-turn compactor only ever touches this trailing turn; everything
    before it is left to ``compact_history``. Returns ``None`` when there is no
    user message at all (nothing to fold).
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and not m.get("tool_calls"):
            return i
    return None


def _scan_rounds(
    messages: list[dict], start: int, end: int
) -> tuple[list[tuple[int, int]], int | None]:
    """Scan ``messages[start:end]`` into tool rounds.

    Returns ``(complete, first_incomplete)`` where ``complete`` is a list of
    ``(round_start, round_end)`` index pairs — ``round_start`` is an assistant
    message carrying ``tool_calls`` and ``round_end`` is the exclusive index
    just past the contiguous ``tool`` results that answer EVERY id in it — and
    ``first_incomplete`` is the index of the first assistant-with-tool_calls
    whose results are missing (``None`` when every round in range is complete).
    """
    complete: list[tuple[int, int]] = []
    first_incomplete: int | None = None
    i = start
    while i < end:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id", "") for tc in m["tool_calls"]]
            j = i + 1
            seen: set = set()
            while j < end and messages[j].get("role") == "tool":
                seen.add(messages[j].get("tool_call_id", ""))
                j += 1
            if all(cid in seen for cid in ids):
                complete.append((i, j))
            elif first_incomplete is None:
                first_incomplete = i
            i = j
        else:
            i += 1
    return complete, first_incomplete


def find_complete_rounds(
    messages: list[dict], start: int, end: int
) -> list[tuple[int, int]]:
    """The ``(round_start, round_end)`` pairs of every COMPLETE round in range."""
    return _scan_rounds(messages, start, end)[0]


def _snippet(text: object, limit: int = ROUND_SNIPPET_CHARS) -> str:
    """Single-line, whitespace-collapsed, length-capped view of ``text``."""
    s = " ".join(str(text or "").split())
    return (s[:limit] + "\u2026") if len(s) > limit else s


def default_round_summarizer(messages: list[dict], start: int, end: int) -> str:
    """Summary text for the folded round region ``[start, end)``.

    The round-granularity analogue of ``default_summarizer``: it walks the region
    round by round and emits one compact line per round (the assistant's own
    pre-call text + the tool name(s) + a truncated result snippet), reusing
    ``extract_done_this_turn`` for any plain-assistant block that carries a
    "Done this turn" report. No LLM call — same text-reuse philosophy as the
    inter-turn path.
    """
    parts: list[str] = []
    ridx = 0
    i = start
    while i < end:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ridx += 1
            names: list[str] = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = _snippet(fn.get("arguments", ""), 60)
                name = str(fn.get("name", "tool"))
                names.append(name if not args or args == "{}" else f"{name}({args})")
            pre = _snippet(m.get("content", ""))
            line = (
                f"Round {ridx}: "
                + (f'"{pre}" ' if pre else "")
                + "called "
                + ", ".join(names)
            )
            j = i + 1
            results: list[str] = []
            while j < end and messages[j].get("role") == "tool":
                results.append(_snippet(messages[j].get("content", "")))
                j += 1
            tail = " | ".join(r for r in results if r)
            if tail:
                line += " \u2192 " + tail
            parts.append("\u2022 " + line)
            i = j
        elif m.get("role") == "assistant":
            block = extract_done_this_turn(messages, i, i + 1) or _snippet(
                m.get("content", "")
            )
            if block:
                parts.append("\u2022 " + block)
            i += 1
        else:
            i += 1
    if parts:
        return "\n".join(parts)
    tool_calls = sum(len(m.get("tool_calls") or []) for m in messages[start:end])
    return (
        f"{tool_calls} earlier tool round(s) were folded from this in-progress "
        "turn; full detail is in the session transcript repo."
    )


def compact_open_turn(
    messages: list[dict],
    keep_last_k_rounds: int = DEFAULT_KEEP_LAST_K_ROUNDS,
    token_threshold: int = DEFAULT_INTRATURN_TOKEN_THRESHOLD,
    round_threshold: int = DEFAULT_INTRATURN_ROUND_THRESHOLD,
    model: str | None = None,
    summarizer: Callable[[list[dict], int, int], str] | None = None,
) -> list[dict]:
    """Compact the CURRENT turn's early tool rounds into ONE summary (copy).

    Result shape when folding happens::

        [... prior msgs ...] + [user (current turn)]
        + [system (any mid-region)] + [ONE "TURN IN PROGRESS" user summary]
        + [last K complete rounds + trailing in-flight msgs]

    The last ``keep_last_k_rounds`` COMPLETE rounds — plus any trailing in-flight
    messages (an assistant whose ``tool_calls`` still await results) — are kept
    verbatim, byte-identical. It is a no-op (fresh, content-identical list) when:
    there is no user message to anchor the turn; the turn has at most
    ``keep_last_k_rounds`` complete rounds; nothing is foldable without crossing
    an INCOMPLETE round; or neither trigger fires (both thresholds <= 0, or the
    round count < ``round_threshold`` while tokens <= ``token_threshold``).
    """
    if not messages:
        return []
    start = find_open_turn_start(messages)
    if start is None or start + 1 >= len(messages):
        return list(messages)

    complete, first_incomplete = _scan_rounds(messages, start + 1, len(messages))
    if len(complete) <= keep_last_k_rounds:
        return list(messages)

    # Fold everything before the K-th-from-last complete round. Only ever cut at
    # a round START (an assistant-with-tool_calls), so the retained tail never
    # begins mid tool_calls/tool pair.
    if keep_last_k_rounds > 0:
        fold_end = complete[-keep_last_k_rounds][0]
    else:
        fold_end = complete[0][0]

    # Never fold ACROSS an incomplete round: if the cut point sits past one, fall
    # back to the complete rounds that precede it.
    if first_incomplete is not None and first_incomplete < fold_end:
        safe = [r for r in complete if r[1] <= first_incomplete]
        if len(safe) <= keep_last_k_rounds:
            return list(messages)
        fold_end = safe[-keep_last_k_rounds][0]

    foldable = [r for r in complete if r[0] < fold_end]
    if not foldable:
        return list(messages)

    round_ok = round_threshold > 0 and len(complete) >= round_threshold
    token_ok = token_threshold > 0 and (
        count_tokens(messages[start:], model=model) > token_threshold
    )
    if not (round_ok or token_ok):
        return list(messages)

    region = messages[start + 1 : fold_end]
    region_sys = [m for m in region if m.get("role") == "system"]
    summ = (summarizer or default_round_summarizer)(messages, start + 1, fold_end)
    if not summ.strip():
        return list(messages)

    summary_msg = {
        "role": "user",
        "content": TURN_IN_PROGRESS_PREFIX.format(k=len(foldable), m=keep_last_k_rounds)
        + "\n"
        + summ,
    }
    return messages[: start + 1] + region_sys + [summary_msg] + messages[fold_end:]
