"""The write client must fail fast and gate on budget (rate-limit plan PR2).

The failure being fixed: ``commit_file`` inherited PyGithub's 10-attempt
``Retry-After`` backoff. On a 403 that can block a ``--workers 1`` process for
~25 minutes, stalling *every* session rather than just the caller.
"""

from __future__ import annotations

import inspect

from app import github_client


def test_default_retry_is_fail_fast_not_pygithub_default():
    # PyGithub's default is 10 attempts. Ours must be small and env-overridable.
    assert github_client.GitHubClient.DEFAULT_RETRY <= 3


def test_init_passes_retry_always():
    src = inspect.getsource(github_client.GitHubClient.__init__)
    assert '"retry"' in src
    assert "DEFAULT_RETRY" in src


def test_explicit_retry_still_wins():
    src = inspect.getsource(github_client.GitHubClient.__init__)
    assert "if retry is None" in src, "explicit retry= must override the default"


def test_every_mutating_method_consults_budget():
    src = inspect.getsource(github_client.GitHubClient)
    for method in (
        "commit_file",
        "delete_file",
        "open_pr",
        "create_branch",
        "delete_branch",
        "merge_pr",
    ):
        body_start = src.index(f"def {method}(")
        nxt = src.find("\n    def ", body_start + 1)
        body = src[body_start : nxt if nxt != -1 else len(src)]
        assert "_respect_budget()" in body, f"{method} must gate on the core budget"


def test_budget_gate_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(github_client.github_ratelimit, "wait_if_exhausted", _boom)
    client = github_client.GitHubClient.__new__(github_client.GitHubClient)
    client._pat = "tok"
    # must swallow, not propagate -- a budget check must never break a write
    assert client._respect_budget() is None
