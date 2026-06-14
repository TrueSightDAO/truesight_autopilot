"""Regression: a double-encoded `attributes` tool arg must not crash the tool loop.

2026-06-14 Kopi Bay onboarding incident — DeepSeek emitted `submit_contribution`
with `attributes` as a JSON *string* instead of a nested object:

    {"event_name": "CONTRIBUTOR ADD EVENT",
     "attributes": "{\\"Contributor Name\\": \\"Nora - Kopi Bar & Bakery\\", ...}"}

`_normalize_submission_labels` is type-hinted `dict` and did `attributes.items()`,
so a str raised `AttributeError: 'str' object has no attribute 'items'` mid
tool-loop. The turn died before the `tool` result was written, leaving an orphan
`tool_calls` that bricked the thread on every subsequent request.
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


def test_stringified_attributes_does_not_crash():
    """A JSON-string `attributes` is parsed, not crashed on."""
    stringified = (
        '{"Contributor Name": "Nora - Kopi Bar & Bakery", '
        '"Contributor Email": "nora@noraharon.com"}'
    )
    # Previously raised AttributeError: 'str' object has no attribute 'items'
    out = m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", stringified)
    assert isinstance(out, dict)
    # The parsed keys are preserved through normalization.
    assert "Contributor Name" in out
    assert out["Contributor Name"] == "Nora - Kopi Bar & Bakery"


def test_dict_attributes_still_work():
    """The normal object form is unchanged."""
    out = m._normalize_submission_labels(
        "CONTRIBUTOR ADD EVENT",
        {"Contributor Name": "Nora", "Contributor Email": "nora@noraharon.com"},
    )
    assert out["Contributor Name"] == "Nora"


def test_garbage_attributes_degrade_gracefully():
    """A non-JSON string or wrong type yields an empty dict, never an exception."""
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", "not json {{{") == {}
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", None) == {}
    assert m._normalize_submission_labels("CONTRIBUTOR ADD EVENT", 123) == {}
