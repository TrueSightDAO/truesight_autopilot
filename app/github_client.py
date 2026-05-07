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
    ) -> str | None:
        """Open a pull request. Returns PR URL or None."""
        try:
            repo = self.g.get_repo(self._full_name(repo_name))
            pr = repo.create_pull(title=title, body=body, head=head, base=base)
            logger.info("Opened PR #%d: %s", pr.number, pr.html_url)
            return pr.html_url
        except Exception as e:
            logger.error("Failed to open PR: %s", e)
            return None

    def merge_pr(
        self,
        repo_name: str,
        pr_number: int,
        merge_method: str = "squash",
    ) -> dict:
        """Merge a pull request. Returns dict with sha, merged, message.

        merge_method: 'merge', 'squash', or 'rebase'.
        Handles already-merged, merge conflicts, and other errors gracefully.
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
