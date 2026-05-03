"""Poll Gmail for actionable emails: GitHub failures, GAS errors, alerts."""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import settings
from .github_client import GitHubClient
from .llm_client import LLMClient, LLMError
from .fix_agent import FixAgent

logger = logging.getLogger("autopilot.email")

# Tier 1: fast rule-based classification
GITHUB_FAILURE_SUBJECTS = re.compile(
    r"(workflow run failed|action required|scheduled workflow failed)", re.IGNORECASE
)
GAS_ERROR_SUBJECTS = re.compile(
    r"(google apps script|script has failed|execution error)", re.IGNORECASE
)
SECURITY_ALERT_SUBJECTS = re.compile(
    r"(security alert|dependabot|vulnerability)", re.IGNORECASE
)


class EmailPoller:
    def __init__(self):
        self.gmail = self._build_gmail_service()
        self.github = GitHubClient()
        self.llm = LLMClient()

    def _build_gmail_service(self):
        token_json = settings.gmail_token_json
        if not token_json:
            logger.warning("GMAIL_TOKEN_JSON not set — email polling disabled")
            return None
        try:
            creds_data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(creds_data)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return build("gmail", "v1", credentials=creds, cache_discovery=False)
        except Exception as e:
            logger.error("Failed to build Gmail service: %s — email polling disabled", e)
            return None

    async def run_loop(self, interval_seconds: int = 300):
        """Poll Gmail every 5 minutes."""
        import asyncio
        if self.gmail is None:
            logger.warning("Email polling skipped — Gmail not configured")
            while True:
                await asyncio.sleep(interval_seconds)
        while True:
            try:
                self.poll_once()
            except Exception as e:
                logger.error("Email poll failed: %s", e)
            await asyncio.sleep(interval_seconds)

    def poll_once(self) -> int:
        """Process unread actionable emails. Returns count processed."""
        if self.gmail is None:
            return 0
        results = self.gmail.users().messages().list(
            userId="me", q="is:unread", maxResults=20
        ).execute()
        messages = results.get("messages", [])
        processed = 0

        for msg_meta in messages:
            msg_id = msg_meta["id"]
            msg = self.gmail.users().messages().get(userId="me", id=msg_id).execute()
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            body = self._extract_body(payload)

            action = self._classify(subject, sender, body)
            if action:
                logger.info("Actionable email: %s from %s — %s", subject, sender, action)
                if not settings.dry_run:
                    self._handle(action, subject, sender, body, msg_id)
                else:
                    logger.info("[dry-run] would handle: %s", action)
                processed += 1

            # Mark as read regardless (we processed it)
            self.gmail.users().messages().modify(
                userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()

        return processed

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        parts = payload.get("parts", [payload])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part["body"].get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            # Recurse into multipart
            if "parts" in part:
                text = self._extract_body(part)
                if text:
                    return text
        return ""

    def _classify(self, subject: str, sender: str, body: str) -> str | None:
        """Tier 1 rule-based classification. Returns action type or None."""
        if GITHUB_FAILURE_SUBJECTS.search(subject):
            return "github_failure"
        if GAS_ERROR_SUBJECTS.search(subject):
            return "gas_error"
        if SECURITY_ALERT_SUBJECTS.search(subject):
            return "security_alert"
        # Tier 2: skip ambiguous (no LLM for classification to save cost)
        return None

    def _handle(self, action: str, subject: str, sender: str, body: str, msg_id: str):
        if action == "github_failure":
            self._handle_github_failure(subject, body)
        elif action == "gas_error":
            logger.info("GAS error detected — handler TODO")
        elif action == "security_alert":
            logger.info("Security alert detected — handler TODO")

    def _handle_github_failure(self, subject: str, body: str):
        """Extract repo/workflow from email, fetch logs, diagnose, open PR."""
        # Naive extraction: look for repo URL patterns
        repo_match = re.search(r"github\.com/([^/]+/[^/]+)/actions", body)
        if not repo_match:
            logger.warning("Could not extract repo from failure email")
            return
        repo = repo_match.group(1)

        run_match = re.search(r"github\.com/[^/]+/[^/]+/actions/runs/(\d+)", body)
        run_id = run_match.group(1) if run_match else None

        logger.info("GitHub failure: repo=%s run_id=%s", repo, run_id)

        if not run_id:
            return

        # Fetch workflow run logs
        log_snippet = self.github.fetch_workflow_log(repo, run_id)
        if not log_snippet:
            logger.warning("No log snippet fetched for %s run %s", repo, run_id)
            return

        # Diagnose
        diagnosis = self.llm.diagnose_github_failure(
            repo=repo,
            workflow_name="unknown",  # TODO: extract from email
            run_url=f"https://github.com/{repo}/actions/runs/{run_id}",
            log_snippet=log_snippet,
        )

        logger.info("Diagnosis: %s", diagnosis.get("root_cause", "N/A"))

        if diagnosis.get("proposed_fix") and not settings.dry_run:
            agent = FixAgent()
            pr_url = agent.run(repo, diagnosis)
            if pr_url:
                logger.info("Opened fix PR: %s", pr_url)
            else:
                logger.info("Fix agent did not produce a PR")
