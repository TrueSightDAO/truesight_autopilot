"""Streaming path must never emit a blank / leaked final answer.

Regression for the 2026-06-16 incident: a round-cap-exhausted turn on the
Public-Key Lookup Cache thread forced a text-only completion, DeepSeek returned
DSML tool-call syntax AS content, `_strip_dsml` emptied it, and the streaming
path (Telegram) had no backstop — so `done.response` was "" and the user saw
"⚠️ Autopilot produced an empty response". The non-streaming `/chat` handler was
already guarded; `_ensure_nonempty_final` makes the streaming path symmetric.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f"app.main import unavailable in this env: {exc}", allow_module_level=True
    )

DSML = m._DSML_OPEN_TOKEN  # raw opener DeepSeek leaks as content


def test_needs_clean_retry_flags_unusable_text():
    assert m._needs_clean_retry("")
    assert m._needs_clean_retry("   \n ")
    assert m._needs_clean_retry("(empty response)")
    assert m._needs_clean_retry("(no response)")
    assert m._needs_clean_retry(f"{DSML}read_repo_file>")
    assert m._needs_clean_retry("<tool_call>list_org_repos</tool_call>")
    # A real answer is fine.
    assert not m._needs_clean_retry("Here is the summary you asked for.")


@pytest.mark.asyncio
async def test_good_answer_passes_through_without_forcing():
    async def _fail():
        pytest.fail("must not force on a good answer")

    text, forced = await m._ensure_nonempty_final(
        "All done — opened PR #42.",
        force_clean=_fail,
    )
    assert text == "All done — opened PR #42."
    assert forced is False


@pytest.mark.asyncio
async def test_blank_then_clean_retry_recovers_real_text():
    async def _recover():
        return "Recovered final answer."

    text, forced = await m._ensure_nonempty_final(
        "",  # stripped-to-empty after DSML removal
        force_clean=_recover,
    )
    assert text == "Recovered final answer."
    assert forced is True


@pytest.mark.asyncio
async def test_dsml_only_retry_still_dsml_falls_back_nonempty():
    # The exact incident: forced completion ALSO returns pure DSML → strips to
    # empty → must land on the fixed non-empty fallback, never "".
    # A clean DSML fragment (what DeepSeek actually leaks) strips fully to "".
    assert m._strip_dsml(f"{DSML}read_repo_file>")[0] == ""

    async def _still_dsml():
        return f"{DSML}read_repo_file>"

    text, forced = await m._ensure_nonempty_final(
        "",
        force_clean=_still_dsml,
    )
    assert forced is True
    assert text == m._EMPTY_TURN_FALLBACK
    assert text.strip()  # the whole point: never blank


@pytest.mark.asyncio
async def test_placeholder_literal_from_retry_falls_back():
    async def _still_placeholder():
        return "(no response)"

    text, forced = await m._ensure_nonempty_final(
        "(empty response)",
        force_clean=_still_placeholder,
    )
    assert forced is True
    assert text == m._EMPTY_TURN_FALLBACK
