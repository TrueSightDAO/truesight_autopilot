"""merge_pr CI gate tests.

Envoy (thread 17181): merge_pr merged PR #372 without checking CI status,
dropping app/signature_ledger_pipeline.py and crashing the service. The fix:
merge_pr must check the PR's combined CI status (Checks API + commit statuses)
before merging and refuse/warn when not green.
"""

from app.github_client import GitHubClient


class FakeStatusRow:
    def __init__(self, context, state):
        self.context = context
        self.state = state
        self.target_url = "https://ci.example/1"


class FakeCheckRun:
    def __init__(self, name, status, conclusion, details_url=""):
        self.name = name
        self.status = status
        self.conclusion = conclusion
        self.details_url = details_url


class FakeCombinedStatus:
    def __init__(self, statuses):
        self.statuses = statuses


class FakeCommit:
    def __init__(self, check_runs, statuses):
        self._runs = check_runs
        self._combined = FakeCombinedStatus(statuses)

    def get_check_runs(self):
        return self._runs

    def get_combined_status(self):
        return self._combined


class FakeRepo:
    def __init__(self, commit):
        self._commit = commit

    def get_commit(self, sha):
        return self._commit


class FakeBase:
    def __init__(self, repo):
        self.repo = repo


class FakePR:
    def __init__(self, sha="abc123", checks=None, statuses=None):
        self.head = type("H", (), {"sha": sha})()
        self.merged = False
        self.draft = False
        self.base = FakeBase(FakeRepo(FakeCommit(checks or [], statuses or [])))

    def mark_ready_for_review(self):
        return None


def _client_with_fake_pr(pr):
    gh = GitHubClient.__new__(GitHubClient)
    gh.g = type(
        "G",
        (),
        {"get_repo": lambda self, n: type("R", (), {"get_pull": lambda self, n: pr})()},
    )()
    return gh


def test_green_checks_merge_allowed():
    pr = FakePR(
        checks=[FakeCheckRun("smoke", "completed", "success")],
        statuses=[FakeStatusRow("unit", "success")],
    )
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is True


def test_failing_check_blocks_merge():
    pr = FakePR(
        checks=[
            FakeCheckRun("smoke", "completed", "failure"),
        ]
    )
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is False
    assert "failing-or-pending" in ci["reason"]


def test_pending_check_blocks_merge():
    pr = FakePR(
        checks=[
            FakeCheckRun("smoke", "in_progress", None),
        ]
    )
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is False
    assert "pending" in ci["reason"]


def test_failed_commit_status_blocks_merge():
    pr = FakePR(statuses=[FakeStatusRow("ci/test", "failure")])
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is False


def test_no_ci_reports_no_ci():
    pr = FakePR(checks=[], statuses=[])
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is False
    assert ci["reason"] == "no-ci"


def test_mixed_passing_checks_green():
    pr = FakePR(
        checks=[
            FakeCheckRun("smoke", "completed", "success"),
            FakeCheckRun("unit", "completed", "success"),
        ],
        statuses=[FakeStatusRow("ci/format", "success")],
    )
    gh = _client_with_fake_pr(pr)
    ci = gh._ci_status(pr)
    assert ci["green"] is True


def test_merge_refuses_when_ci_not_green():
    pr = FakePR(checks=[FakeCheckRun("smoke", "completed", "failure")])
    gh = _client_with_fake_pr(pr)
    result = gh.merge_pr("truesight_autopilot", 1)
    assert result["merged"] is False
    assert "Refusing to merge" in result["message"]


# --- fallback: Checks API unreadable (403) but repo has no workflows ---


class _FakeGithubException(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class _FakeContent:
    def __init__(self, name):
        self.name = name


class _FakeRepoWithContents:
    def __init__(self, commit, contents_result):
        self._commit = commit
        self._contents_result = contents_result

    def get_commit(self, sha):
        return self._commit

    def get_contents(self, path, ref=None):
        if isinstance(self._contents_result, Exception):
            raise self._contents_result
        return self._contents_result


class _FakeBase2:
    def __init__(self, repo):
        self.repo = repo


class _FakePR2:
    def __init__(self, contents_result):
        self.head = type("H", (), {"sha": "abc123"})()

        class _Commit:
            def get_check_runs(self_):
                raise _FakeGithubException(403)

            def get_combined_status(self_):
                raise _FakeGithubException(403)

        repo = _FakeRepoWithContents(_Commit(), contents_result)
        self.base = _FakeBase2(repo)
        self.merged = False
        self.draft = False


def _client_for_pr2(pr):
    gh = GitHubClient.__new__(GitHubClient)
    gh.g = type(
        "G",
        (),
        {"get_repo": lambda self, n: _FakeRepoWithContents(None, None)},
    )()
    return gh


def test_checks_403_and_no_workflows_falls_back_to_no_ci():
    # get_contents on .github/workflows -> 404 => repo has no CI.
    pr = _FakePR2(_FakeGithubException(404))
    gh = _client_for_pr2(pr)
    ci = gh._ci_status(pr)
    assert ci["reason"] == "no-ci"
    assert ci["green"] is False


def test_checks_403_with_workflows_still_refuses():
    # Workflows exist => genuine CI we merely cannot read -> stay blocked.
    pr = _FakePR2([_FakeContent("test.yml")])
    gh = _client_for_pr2(pr)
    ci = gh._ci_status(pr)
    assert "ci-unavailable" in ci["reason"]


def test_repo_has_workflows_true_and_false():
    pr = _FakePR2([_FakeContent("smoke.yml"), _FakeContent("README.md")])
    gh = _client_for_pr2(pr)
    assert gh._repo_has_workflows(pr) is True

    pr2 = _FakePR2(_FakeGithubException(404))
    assert gh._repo_has_workflows(pr2) is False

    # Unknown error (e.g. 500) -> conservative True.
    pr3 = _FakePR2(_FakeGithubException(500))
    assert gh._repo_has_workflows(pr3) is True


def test_repo_has_workflows_ignores_non_yaml():
    pr = _FakePR2([_FakeContent("notes.txt")])
    gh = _client_for_pr2(pr)
    assert gh._repo_has_workflows(pr) is False
