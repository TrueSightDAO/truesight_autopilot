"""Tests for rubric-auto-computed TDG in DAO contribution submissions.

Guards Gary's standing rule (2026-09-10, thread 24442,
DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md rule 5): never hard-default TDG to 0 --
use the auto-computed rubric value instead.
"""

from __future__ import annotations

import app.tools.dao_submission as ds


def test_rubric_time_minutes_600_is_1000():
    assert ds.compute_tdg_from_rubric("Time (Minutes)", "600") == "1000.00"


def test_rubric_time_one_hour_is_100():
    assert ds.compute_tdg_from_rubric("Time (Minutes)", "60") == "100.00"


def test_rubric_usd_is_one_to_one():
    assert ds.compute_tdg_from_rubric("USD", "12") == "12.00"


def test_rubric_zero_minutes_is_zero():
    assert ds.compute_tdg_from_rubric("Time (Minutes)", "0") == "0.00"


def test_rubric_unknown_type_returns_none_not_zero():
    assert ds.compute_tdg_from_rubric("Bogus Type", "5") is None


def test_rubric_non_numeric_amount_returns_none():
    assert ds.compute_tdg_from_rubric("Time (Minutes)", "abc") is None


def _capture_cmd(monkeypatch):
    calls = []

    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _R()

    monkeypatch.setattr(ds.subprocess, "run", _fake_run)
    return calls


def test_cli_omits_tdg_and_amount_uses_minutes(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    ds.submit_ai_agent_contribution(
        title="t",
        body="b",
        pr_urls=["https://github.com/TrueSightDAO/x/pull/1"],
        amount="600",
        contribution_type="Time (Minutes)",
    )
    cmd = calls[-1]
    assert "--tdg-issued" not in cmd, cmd
    assert "--amount" not in cmd, cmd
    assert "--type" in cmd and cmd[cmd.index("--type") + 1] == "Time (Minutes)"
    assert "--minutes" in cmd and cmd[cmd.index("--minutes") + 1] == "600"


def test_cli_uses_usd_for_usd_type(monkeypatch):
    calls = _capture_cmd(monkeypatch)
    ds.submit_ai_agent_contribution(
        title="t",
        body="b",
        pr_urls=["https://github.com/TrueSightDAO/x/pull/1"],
        amount="12",
        contribution_type="USD",
    )
    cmd = calls[-1]
    assert "--usd" in cmd and cmd[cmd.index("--usd") + 1] == "12"
    assert "--minutes" not in cmd
