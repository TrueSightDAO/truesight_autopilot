"""Regression: create_dao_submission must auto-compute TDG, never default to 0.

2026-09 incident -- two contribution reports (Cristo Rei, Sitio Torres FSVP)
were filed with `TDG Issued = 0` because the `create_dao_submission` handler
hard-defaulted `tdg_issued` to the string "0" and never consulted the rubric.

`_resolve_tdg_issued` fixes that: TDG is DERIVED from Type + Amount via the
dao_client rubric (SSOT); a governor override wins ONLY when it is an explicit,
non-zero figure. Absent / "" / "0" / unparseable all fall through to
auto-compute.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

from app.main import _resolve_tdg_issued  # noqa: E402


def test_absent_auto_computes():
    assert _resolve_tdg_issued(None, "Time (Minutes)", "60") == "100.00"


def test_zero_string_auto_computes():
    assert _resolve_tdg_issued("0", "Time (Minutes)", "60") == "100.00"


def test_zero_float_auto_computes():
    assert _resolve_tdg_issued("0.00", "Time (Minutes)", "30") == "50.00"


def test_empty_auto_computes():
    assert _resolve_tdg_issued("", "USD", "25") == "25.00"


def test_explicit_nonzero_override_honoured():
    assert _resolve_tdg_issued("42", "Time (Minutes)", "60") == "42.00"


def test_explicit_currency_override_honoured():
    assert _resolve_tdg_issued("$1,234.5", "USD", "10") == "1234.50"


def test_unparseable_auto_computes():
    assert _resolve_tdg_issued("n/a", "Time (Minutes)", "30") == "50.00"


def test_unknown_type_does_not_crash():
    # Must never raise mid-tool-loop; returns a string.
    assert isinstance(_resolve_tdg_issued(None, "Bogus", "10"), str)


def test_rubric_import_failure_is_loud(monkeypatch, caplog):
    """A missing rubric module must be an ERROR, not a silent zero.

    2026-10 live incident: the installed ``truesight_dao_client`` predated the
    commit that added ``rubric.py``, so the import raised ModuleNotFoundError on
    every call. The blanket ``except Exception`` turned that into "0.00" and
    nothing was logged, so every auto-computed contribution was quietly zeroed
    while this test file stayed red and unnoticed.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "truesight_dao_client.rubric":
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with caplog.at_level("ERROR"):
        result = _resolve_tdg_issued(None, "Time (Minutes)", "60")

    # Still returns a string so the tool loop is never bricked mid-turn.
    assert isinstance(result, str)
    # But it must SCREAM, and point at the remediation.
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "rubric import failure was swallowed silently"
    assert "RUBRIC UNAVAILABLE" in caplog.text
    assert "force-reinstall" in caplog.text
