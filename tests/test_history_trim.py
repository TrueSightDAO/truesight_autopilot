"""Token-aware history trim — root-cause fix for the 780/2622 context-overflow brick.

The old char-only trim (400K chars) assumed ~4 chars/token, but tool-result/key-dense
content runs ~2 chars/token → 400K chars was ~200K tokens, over DeepSeek's 131K window,
so the LLM overflowed (empty response) BEFORE the trim ever fired.
"""

from __future__ import annotations

import copy
import os
import tempfile

import pytest

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

try:
    import app.main as m
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.main import unavailable: {exc}", allow_module_level=True)


def test_trim_skips_small_history():
    h = [
        {"role": "system", "content": "[ROLE: general]"},
        {"role": "user", "content": "hi"},
    ]
    before = copy.deepcopy(h)
    m._trim_history_to_budget(h)
    assert h == before  # under the char gate → untouched, no token counting


def test_trim_drops_oldest_preserves_system_and_recent(monkeypatch):
    monkeypatch.setattr(m, "_HISTORY_CHAR_SKIP", 0)  # force past the cheap fast-path
    monkeypatch.setattr(m, "_HISTORY_TOKEN_BUDGET", 2500)
    monkeypatch.setattr(m, "_history_token_count", lambda msgs: 1000 * len(msgs))
    h = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"m{i}"} for i in range(6)
    ]
    m._trim_history_to_budget(h)
    assert h[0]["content"] == "sys"  # system/role tag preserved at the front
    assert h[-1]["content"] == "m5"  # most-recent message kept
    assert len(h) < 7  # oldest were dropped
    assert "m0" not in [x["content"] for x in h]  # the oldest is gone


def test_trim_noop_when_under_budget(monkeypatch):
    monkeypatch.setattr(m, "_HISTORY_CHAR_SKIP", 0)
    monkeypatch.setattr(m, "_HISTORY_TOKEN_BUDGET", 999_999)
    monkeypatch.setattr(m, "_history_token_count", lambda msgs: 100 * len(msgs))
    h = [{"role": "user", "content": f"m{i}"} for i in range(5)]
    before = copy.deepcopy(h)
    m._trim_history_to_budget(h)
    assert h == before  # over the char gate but under token budget → untouched
