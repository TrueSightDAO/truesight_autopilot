"""PR3 — detailed per-turn completion reports (invariant 7).

A turn that runs side-effecting tools must end with an explicit "what I did"
report, so when several queued instructions run as back-to-back turns the
governor sees what each accomplished before the next begins. Read-only turns
get no report (no clutter).
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


def test_no_report_for_readonly_turn():
    trace = [
        {"name": "web_search", "result": "..."},
        {"name": "read_repo_file", "result": "x"},
    ]
    assert m._build_turn_report(trace) == ""
    assert m._append_turn_report("here you go", {"tool_trace": trace}) == "here you go"


def test_report_lists_side_effects_with_urls():
    trace = [
        {"name": "read_repo_file", "result": "contents"},  # read-only, omitted
        {
            "name": "open_fix_pr",
            "result": "Opened PR: https://github.com/TrueSightDAO/x/pull/7",
        },
        {"name": "deploy_autopilot", "result": "deployed ok\nrestarted service"},
    ]
    report = m._build_turn_report(trace)
    assert "Done this turn" in report
    assert "open fix pr" in report
    assert "https://github.com/TrueSightDAO/x/pull/7" in report  # URL preferred
    assert "deploy autopilot" in report
    assert "deployed ok" in report  # first line when no URL
    assert "read repo file" not in report  # read-only omitted


def test_append_joins_report_to_text():
    trace = [{"name": "submit_contribution", "result": "logged 30 TDG"}]
    out = m._append_turn_report("All set.", {"tool_trace": trace})
    assert out.startswith("All set.")
    assert "Done this turn" in out
    assert "submit contribution" in out
    assert "logged 30 TDG" in out


def test_summarise_prefers_url_then_first_line():
    assert (
        m._summarise_tool_result("noise\nhttps://a.test/pr/1\nmore")
        == "https://a.test/pr/1"
    )
    assert (
        m._summarise_tool_result("\n\n  first real line\nsecond") == "first real line"
    )
    assert m._summarise_tool_result("") == ""


# ── regressions for the 2026-06-10 garbage report ────────────────────────────


def test_json_result_never_shows_bare_brace():
    # ssh_run returns a JSON object — must not surface a lone "{".
    out = m._summarise_tool_result('{\n  "exit_code": 0,\n  "stdout": "ok done"\n}')
    assert out != "{"
    assert out == "ok done"  # pulls the salient field
    # malformed/huge JSON that won't parse must still never yield "{"
    assert m._summarise_tool_result('{ "stdout": "truncated...') != "{"


def test_url_detail_strips_trailing_backslash_n():
    # The exact leak: a URL with a literal \n glued on.
    assert (
        m._summarise_tool_result("pushed to https://github.com/TrueSightDAO/x\\nmore")
        == "https://github.com/TrueSightDAO/x"
    )


def test_repeated_tool_calls_are_grouped_with_count():
    trace = [
        {"name": "ssh_run", "args": {"command": f"echo {i}"}, "result": "{}"}
        for i in range(16)
    ]
    trace.append(
        {"name": "open_fix_pr", "result": "https://github.com/TrueSightDAO/x/pull/150"}
    )
    report = m._build_turn_report(trace)
    # 16 ssh_run calls collapse to ONE grouped line, not sixteen.
    assert report.count("ssh run") == 1
    assert "×16" in report
    assert report.count("→ {") == 0  # no bare-brace junk
    assert "pull/150" in report
    # the whole report stays compact (was 26+ lines / blew past Telegram's cap)
    assert len(report.splitlines()) <= 8


def test_command_tools_show_the_command_not_the_json():
    trace = [
        {
            "name": "ssh_run",
            "args": {"command": "git checkout main"},
            "result": '{"exit_code":0}',
        }
    ]
    report = m._build_turn_report(trace)
    assert "git checkout main" in report
