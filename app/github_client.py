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
