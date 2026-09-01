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
