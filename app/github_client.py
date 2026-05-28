"""GitHub API client: read repos, create branches, commit, open PRs.

IMPORTANT: This client uses the GitHub Content API exclusively.
It does NOT clone repos. Repos like .github are large static assets
that should never be pulled locally — always read/write via the API."""
from __future__ import annotations

import logging
from typing import Any

from github import Github, Auth

from .config import settings

logger = logging.getLogger("autopilot.github")


class GitHubClient:
    ORG = "TrueSightDAO"

    def __init__(self):
        if not settings.github_pat:
            raise RuntimeError("TRUESIGHT_DAO_AUTOPILOT not set")
        auth = Auth.Token(settings.github_pat)
        self.g = Github(auth=auth)
        self._user = self.g.get_user()
        logger.info("GitHub client authenticated as %s", self._user.login)

    def _full_name(self, repo_name: str) -> str:
        """Prepend ORG if repo_name doesn't already include a slash."""
        return repo_name if "/" in repo_name else f"{self.ORG}/{repo_name}"

    def list_org_repos(self, org: str = "TrueSightDAO") -> list[dict[str, str]]:
        """List all repos in the org (name, description, default_branch)."""
        try:
            org_obj = self.g.get_organization(org)
            repos = org_obj.get_repos(type="all", sort="full_name")
            return [
                {
                    "name": r.name,
                    "description": r.description or "",
                    "default_branch": r.default_branch,
                    "private": r.private,
                    "archived": r.archived,
                }
                for r in repos
                if not r.archived
            ]
        except Exception as e:
            logger.error("Failed to list org repos: %s", e)
            return []

    def read_file(self, repo_name: str, path: str, ref: str = "main") -> dict[str, Any]:
        """Read a file (or directory listing) from a repo."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            content = repo.get_contents(path, ref=ref)
            if isinstance(content, list):
                return {
                    "type": "directory",
                    "entries": [
                        {"name": item.name, "type": item.type, "path": item.path}
                        for item in content
                    ],
                }
            decoded = content.decoded_content.decode("utf-8", errors="replace")
            return {
                "type": "file",
                "content": decoded,
                "size": content.size,
                "sha": content.sha,
                "url": content.html_url,
            }
        except Exception as e:
            logger.error("Failed to read %s/%s: %s", repo_name, path, e)
            return {"type": "error", "error": str(e)}

    def fetch_workflow_log(self, repo_name: str, run_id: str, max_lines: int = 200) -> str:
        """Fetch the tail of a workflow run log."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            run = repo.get_workflow_run(int(run_id))
            # GitHub API doesn't give raw logs directly; we get the jobs
            jobs = run.jobs()
            lines: list[str] = []
            for job in jobs:
                logs_url = job.logs_url()
                # logs_url is a redirect to a tarball; we'd need to download and parse
                # For MVP, use the job name + conclusion as proxy
                lines.append(f"Job: {job.name} — {job.conclusion}")
                for step in job.steps:
                    if step.conclusion == "failure":
                        lines.append(f"  FAILED STEP: {step.name}")
            return "\n".join(lines[:max_lines])
        except Exception as e:
            logger.error("Failed to fetch workflow log: %s", e)
            return ""

    def create_branch(self, repo_name: str, base_branch: str, new_branch: str) -> bool:
        """Create a new branch from base."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            base = repo.get_branch(base_branch)
            repo.create_git_ref(ref=f"refs/heads/{new_branch}", sha=base.commit.sha)
            logger.info("Created branch %s on %s", new_branch, repo_name)
            return True
        except Exception as e:
            logger.error("Failed to create branch: %s", e)
            return False

    def delete_branch(self, repo_name: str, branch: str) -> bool:
        """Delete a branch ref. Returns True on success, False otherwise."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            ref = repo.get_git_ref(f"heads/{branch}")
            ref.delete()
            logger.info("Deleted branch %s on %s", branch, repo_name)
            return True
        except Exception as e:
            logger.warning("Failed to delete branch %s on %s: %s", branch, repo_name, e)
            return False

    def list_branches_matching(self, repo_name: str, prefix: str) -> list[dict[str, Any]]:
        """List branches whose name starts with `prefix`. Returns a list of
        {name, sha, last_commit_at (iso8601)} dicts. Used by the autopilot
        branch janitor to find orphan `autopilot/fix-*` branches."""
        out: list[dict[str, Any]] = []
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            for b in repo.get_branches():
                if not b.name.startswith(prefix):
                    continue
                last_at = ""
                try:
                    last_at = b.commit.commit.author.date.isoformat()
                except Exception:
                    pass
                out.append({"name": b.name, "sha": b.commit.sha, "last_commit_at": last_at})
        except Exception as e:
            logger.warning("Failed to list branches on %s: %s", repo_name, e)
        return out

    def commit_file(
        self,
        repo_name: str,
        branch: str,
        path: str,
        content: str,
        message: str,
    ) -> bool:
        """Commit a file to a branch."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            # Check if file exists to get sha for update
            try:
                existing = repo.get_contents(path, ref=branch)
                repo.update_file(
                    path=path,
                    message=message,
                    content=content,
                    sha=existing.sha,
                    branch=branch,
                )
            except Exception:
                repo.create_file(
                    path=path,
                    message=message,
                    content=content,
                    branch=branch,
                )
            logger.info("Committed %s to %s:%s", path, repo_name, branch)
            return True
        except Exception as e:
            logger.error("Failed to commit file: %s", e)
            return False

    def delete_file(
        self, repo_name: str, branch: str, path: str,
    ) -> bool:
        """Delete a file from a branch."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            existing = repo.get_contents(path, ref=branch)
            repo.delete_file(
                path=path,
                message=f"[autopilot] Delete {path}",
                sha=existing.sha,
                branch=branch,
            )
            logger.info("Deleted %s from %s:%s", path, repo_name, branch)
            return True
        except Exception as e:
            logger.error("Failed to delete file: %s", e)
            return False

    def open_pr(
        self,
        repo_name: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = True,
        labels: list[str] | None = None,
    ) -> str | None:
        """Open a pull request. Returns PR URL or None.

        ``labels`` are applied after the PR is created. Missing labels are
        auto-created in the target repo (PyGithub's add_to_labels with a
        string raises if the label doesn't exist; we create-if-missing first).
        Label failures are logged but do NOT roll back the PR — the operator
        review is the gate, the label is just a search/filter aid.
        """
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            pr = repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)
            logger.info("Opened PR #%d: %s", pr.number, pr.html_url)
            if labels:
                self._apply_labels(repo, pr, labels)
            return pr.html_url
        except Exception as e:
            logger.error("Failed to open PR: %s", e)
            return None

    def _apply_labels(self, repo, pr, labels: list[str]) -> None:
        """Idempotently ensure each label exists in the repo, then attach to the PR.

        Default visual: warm yellow #f4a300 — matches the convention for
        operator-attention items. Operators can recolor via the GitHub UI
        without breaking the autopilot workflow.
        """
        for name in labels:
            try:
                try:
                    repo.get_label(name)
                except Exception:
                    repo.create_label(name=name, color="f4a300",
                                      description="Created by truesight_autopilot")
                pr.add_to_labels(name)
            except Exception as e:
                logger.warning("Could not apply label %r to PR #%d: %s", name, pr.number, e)

    def mark_pr_ready_for_review(
        self,
        repo_name: str,
        pr_number: int,
    ) -> dict:
        """Promote a draft PR to ready-for-review. Returns dict with status + draft flag.

        Wraps PyGithub's ``PullRequest.mark_ready_for_review`` (GraphQL under the hood).
        Idempotent — calling on an already-ready PR is a no-op and returns ``already_ready``.
        """
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            pr = repo.get_pull(pr_number)
            if not pr.draft:
                return {
                    "status": "already_ready",
                    "draft": False,
                    "message": f"PR #{pr_number} is already marked ready for review.",
                }
            pr.mark_ready_for_review()
            logger.info("Marked PR #%d on %s ready for review", pr_number, repo_name)
            return {
                "status": "promoted",
                "draft": False,
                "message": f"PR #{pr_number} on {repo_name} marked ready for review.",
            }
        except Exception as e:
            logger.error("Failed to mark PR #%d on %s ready: %s", pr_number, repo_name, e)
            return {
                "status": "error",
                "draft": True,
                "message": f"Failed to mark PR #{pr_number} ready: {e}",
            }

    def merge_pr(
        self,
        repo_name: str,
        pr_number: int,
        merge_method: str = "squash",
    ) -> dict:
        """Merge a pull request. Returns dict with sha, merged, message.

        merge_method: 'merge', 'squash', or 'rebase'.
        Handles already-merged, merge conflicts, and other errors gracefully.
        Auto-promotes a draft PR to ready-for-review before merging — autopilot
        opens its own PRs as draft (the safe default) and GitHub refuses to
        merge drafts with a 405; the auto-promote removes the manual step at
        the merge moment. The PR's draft history stays visible in the timeline.
        """
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            pr = repo.get_pull(pr_number)
            if pr.merged:
                return {
                    "sha": pr.merge_commit_sha or "",
                    "merged": True,
                    "message": f"PR #{pr_number} was already merged.",
                }
            if pr.draft:
                try:
                    pr.mark_ready_for_review()
                    logger.info("Auto-promoted draft PR #%d on %s before merge", pr_number, repo_name)
                    # Re-fetch PR state after promotion so PyGithub's mergeability
                    # cache catches up to the new state.
                    pr = repo.get_pull(pr_number)
                except Exception as e:
                    logger.warning("Failed to auto-promote draft PR #%d on %s: %s", pr_number, repo_name, e)
            result = pr.merge(merge_method=merge_method)
            logger.info(
                "Merged PR #%d on %s (method=%s, sha=%s)",
                pr_number, repo_name, merge_method, result.sha,
            )
            return {
                "sha": result.sha,
                "merged": result.merged,
                "message": result.message,
            }
        except Exception as e:
            error_msg = str(e)
            logger.error("Failed to merge PR #%d on %s: %s", pr_number, repo_name, error_msg)
            return {
                "sha": "",
                "merged": False,
                "message": f"Failed to merge PR #{pr_number}: {error_msg}",
            }

    def list_prs(
        self,
        repo_name: str,
        state: str = "all",
        limit: int = 20,
    ) -> list[dict]:
        """List pull requests on a repo. Returns list of dicts with number, title, state, merged_at, url."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            pulls = repo.get_pulls(state=state, sort="updated", direction="desc")
            result = []
            for pr in pulls[:limit]:
                result.append({
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                    "url": pr.html_url,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                })
            return result
        except Exception as e:
            logger.error("Failed to list PRs on %s: %s", repo_name, e)
            return []
