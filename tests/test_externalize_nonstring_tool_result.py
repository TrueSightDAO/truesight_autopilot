"""Regression: a non-string tool result must not crash the tool loop.

2026-06-14 — after the submit_contribution fix unblocked the Kopi Bay thread,
Sophia called the new CM3 tool `recall_context`, which returns a **dict**. The
CM1 `_externalize_tool_result` early-returned the dict unchanged, so:
  - `result_text[:300]` (a log slice) raised `TypeError: unhashable type: 'slice'`
  - a dict leaked into the `tool` message `content` (would break the next call)
The streaming turn crashed mid-chunk → the Telegram adapter surfaced
"peer closed connection without sending complete message body
(incomplete chunked read)".

_externalize_tool_result must ALWAYS return a string.
"""

from __future__ import annotations

import json
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


def test_dict_result_is_stringified_not_returned_raw():
    """A dict tool result (e.g. recall_context) becomes a JSON string."""
    result = {"status": "error", "reason": "no session context to search"}
    out = m._externalize_tool_result(result, "call_1", "sess_x", "recall_context")
    assert isinstance(out, str)
    # round-trips back to the original structure
    assert json.loads(out) == result
    # and the thing that used to crash now works
    _ = out[:300]  # no TypeError: unhashable type: 'slice'


def test_list_result_is_stringified():
    out = m._externalize_tool_result([1, 2, {"a": "b"}], "call_2", "sess_x", "tool")
    assert isinstance(out, str)
    assert json.loads(out) == [1, 2, {"a": "b"}]


def test_small_string_passthrough_unchanged():
    out = m._externalize_tool_result("hello", "call_3", "sess_x", "tool")
    assert out == "hello"


def test_large_dict_is_externalized_to_summary_string():
    """A big non-string result still gets summarized to a string (+ artifact handle)."""
    big = {"items": ["x" * 100 for _ in range(200)]}  # well over 8K chars as JSON
    out = m._externalize_tool_result(big, "call_4", "sess_x", "recall_context")
    assert isinstance(out, str)
    assert len(out) <= m._MAX_TOOL_RESULT_CHARS + 500  # summary, not the full blob
    _ = out[:300]  # never crashes
