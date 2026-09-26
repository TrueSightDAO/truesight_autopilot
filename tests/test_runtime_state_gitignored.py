"""Guard: machine-owned runtime state under the deploy tree must be git-ignored.

``deploy_autopilot`` runs ``git reset --hard origin/main && git clean -fd``. Any
untracked-and-unignored state file inside the repo is therefore DELETED on every
deploy. This bit the project twice (2026-06-15 vault wipe, 2026-09-14 followups
reset). This test pins the current set so a regression fails loudly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Machine-owned files written into the deploy tree at runtime.
MUST_SURVIVE_DEPLOY = (
    "data/thread_goals.jsonl",
    "data/email_inbox_watch_state.json",
    "data/followups_state.json",
    "data/attention_watchdog_state.json",
)


def _is_ignored(rel: str) -> bool:
    r = subprocess.run(
        ["git", "check-ignore", "-q", rel],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    # exit 0 => ignored; 1 => not ignored; 128 => not a git repo (skip)
    if r.returncode == 128:
        return True
    return r.returncode == 0


def test_runtime_state_files_are_gitignored():
    missing = [p for p in MUST_SURVIVE_DEPLOY if not _is_ignored(p)]
    assert not missing, (
        "These runtime-state files are NOT git-ignored, so `git clean -fd` "
        f"will delete them on deploy: {missing}. Add them to .gitignore."
    )
