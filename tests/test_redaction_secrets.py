"""Regression guard for _redact_secrets (public transcript publish scrubber).

Fine-grained GitHub PATs (github_pat_...) previously had NO redaction rule —
only the classic ghp_/gho_/ghs_/ghu_/ghr_ formats were covered. A pasted
fine-grained PAT would therefore have been published verbatim. These tests
pin the fix.
"""

from app.main import _redact_secrets


def test_redacts_fine_grained_github_pat():
    token = "github_pat_11AAIHROQ0" + "a" * 71  # 93-char fine-grained PAT shape
    redacted, matched = _redact_secrets(f"token={token}")
    assert token not in redacted
    assert "REDACTED:GITHUB_FINE_GRAINED_PAT" in redacted
    assert "GITHUB_FINE_GRAINED_PAT" in matched


def test_redacts_classic_github_token():
    token = "ghp_" + "A" * 40
    redacted, _ = _redact_secrets(f"Bearer {token}")
    assert token not in redacted
    assert "REDACTED:GITHUB_TOKEN" in redacted


def test_redacts_env_style_value():
    redacted, _ = _redact_secrets("SOME_SECRET_KEY=abcdefghij0123456789")
    assert "abcdefghij0123456789" not in redacted
    assert "REDACTED:ENV_VALUE" in redacted


def test_leaves_ordinary_text_untouched():
    text = "the cacao harvest looks good this week"
    assert _redact_secrets(text)[0] == text
