"""Guard: tool-call log data must be redacted before it reaches the journal.

Regression (2026-09-25): the ``TOOL CALL`` log line and the live-progress
``current_arg`` wrote raw ``func_args`` verbatim, so a tool call whose argv
embedded ``git clone https://x-access-token:<PAT>@github.com/...`` wrote the
PAT into the *persistent* systemd journal. ``_redact_for_log`` closes that
path by reusing the single-source-of-truth redaction rules.
"""

from app.main import _redact_for_log


def test_redacts_pat_in_clone_url():
    args = {
        "command": "git clone https://x-access-token:github_pat_11AAIHROQ0"
        + "a" * 71
        + "@github.com/TrueSightDAO/truesight_autopilot.git"
    }
    out = _redact_for_log(args)
    assert "github_pat_" not in out
    assert "x-access-token:" in out  # surrounding context preserved
    assert "REDACTED" in out


def test_redacts_classic_ghp_token_in_string():
    out = _redact_for_log("token=ghp_" + "A" * 40)
    assert "ghp_" not in out
    assert "REDACTED" in out


def test_truncates_to_limit():
    out = _redact_for_log({"k": "x" * 1000}, limit=50)
    assert len(out) <= 50


def test_handles_non_json_serialisable():
    out = _redact_for_log(object())
    assert isinstance(out, str)
