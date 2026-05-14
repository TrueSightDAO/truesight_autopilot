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
# Bugsnag-emitted error notifications (sender + subject signature). Bugsnag
# sends from support@bugsnag.com or notifications@bugsnag.com with subjects
# like "[Bugsnag] Error in <Project> - <Message>" or "New error in <Project>".
BUGSNAG_SENDER = re.compile(r"@bugsnag\.com", re.IGNORECASE)
BUGSNAG_SUBJECTS = re.compile(
    r"(\[bugsnag\]|new error|error in|reopened|spike in errors)", re.IGNORECASE
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
            # users.messages.get does NOT modify read state (only modify/UI does),
            # so fetching here is safe — the message stays UNREAD until we decide.
            msg = self.gmail.users().messages().get(userId="me", id=msg_id).execute()
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            body = self._extract_body(payload)

            action = self._classify(subject, sender, body)
            if not action:
                # Not actionable for autopilot — leave UNREAD so the operator
                # reads it normally in Gmail. Previously we marked everything
                # read after polling, which silently swallowed mail autopilot
                # had no business touching.
                continue

            logger.info("Actionable email: %s from %s — %s", subject, sender, action)
            if settings.dry_run:
                # In dry-run we don't actually fix anything, so leaving the
                # message UNREAD preserves the operator's ability to verify.
                logger.info("[dry-run] would handle: %s — leaving UNREAD", action)
            else:
                self._handle(action, subject, sender, body, msg_id)
                # Only after autopilot has actually handled the message is it
                # safe to mark read — the action is captured (PR opened, log
                # filed, etc.) and the email no longer needs operator attention.
                self.gmail.users().messages().modify(
                    userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
                ).execute()
            processed += 1

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
        # Bugsnag classification checks sender first — narrower than subject
        # heuristic, avoids false positives on humans forwarding bugsnag-style
        # subjects.
        if BUGSNAG_SENDER.search(sender) and BUGSNAG_SUBJECTS.search(subject):
            return "bugsnag_error"
        if GITHUB_FAILURE_SUBJECTS.search(subject):
            return "github_failure"
        if GAS_ERROR_SUBJECTS.search(subject):
            return "gas_error"
        if SECURITY_ALERT_SUBJECTS.search(subject):
            return "security_alert"
        # Tier 2: skip ambiguous (no LLM for classification to save cost)
        return None

    # Gmail label applied to source emails whose autopilot-triage opened a PR.
    # Mirrors the GitHub PR label so Gary can find both surfaces with one
    # search ("AI/proposed fix" in Gmail = inbox view of awaiting approvals).
    PROPOSED_FIX_GMAIL_LABEL = "AI/proposed fix"

    def _handle(self, action: str, subject: str, sender: str, body: str, msg_id: str):
        pr_url: str | None = None
        if action == "github_failure":
            pr_url = self._handle_github_failure(subject, body)
        elif action == "bugsnag_error":
            pr_url = self._handle_bugsnag_error(subject, body)
        elif action == "gas_error":
            logger.info("GAS error detected — handler TODO")
        elif action == "security_alert":
            logger.info("Security alert detected — handler TODO")

        if pr_url and msg_id:
            self._apply_gmail_label(msg_id, self.PROPOSED_FIX_GMAIL_LABEL)

    def _apply_gmail_label(self, msg_id: str, label_name: str) -> None:
        """Idempotently get-or-create a Gmail label, then attach to the message.

        Failures are logged but never roll back the PR — the GitHub PR is the
        authoritative artifact, the Gmail label is just an inbox-side index.
        """
        try:
            label_id = self._get_or_create_label(label_name)
            if not label_id:
                return
            self.gmail.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [label_id]},
            ).execute()
            logger.info("Applied Gmail label %r to message %s", label_name, msg_id)
        except Exception as e:
            logger.warning("Could not apply Gmail label %r to message %s: %s",
                           label_name, msg_id, e)

    def _get_or_create_label(self, name: str) -> str | None:
        """Return Gmail label ID for `name`, creating it if missing."""
        try:
            existing = self.gmail.users().labels().list(userId="me").execute()
            for lbl in existing.get("labels", []):
                if lbl.get("name") == name:
                    return lbl.get("id")
            created = self.gmail.users().labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
            return created.get("id")
        except Exception as e:
            logger.warning("get_or_create label %r failed: %s", name, e)
            return None

    def _handle_github_failure(self, subject: str, body: str) -> str | None:
        """Extract repo/workflow from email, fetch logs, diagnose, open PR.

        Returns the PR URL when a fix PR is opened, else None. Caller uses
        the return value to decide whether to apply the AI/proposed fix
        Gmail label to the source email.
        """
        # Naive extraction: look for repo URL patterns
        repo_match = re.search(r"github\.com/([^/]+/[^/]+)/actions", body)
        if not repo_match:
            logger.warning("Could not extract repo from failure email")
            return None
        repo = repo_match.group(1)

        run_match = re.search(r"github\.com/[^/]+/[^/]+/actions/runs/(\d+)", body)
        run_id = run_match.group(1) if run_match else None

        logger.info("GitHub failure: repo=%s run_id=%s", repo, run_id)

        if not run_id:
            return None

        # Fetch workflow run logs
        log_snippet = self.github.fetch_workflow_log(repo, run_id)
        if not log_snippet:
            logger.warning("No log snippet fetched for %s run %s", repo, run_id)
            return None

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
                return pr_url
            logger.info("Fix agent did not produce a PR")
        return None

    def _handle_bugsnag_error(self, subject: str, body: str) -> str | None:
        """Triage a Bugsnag error notification email.

        v0 scope: parse + log only. Bugsnag emails carry a project name,
        error class, and stack trace, but not enough metadata to map to a
        specific GitHub repo without an operator-maintained mapping
        (Bugsnag-project-id -> github-repo). Until that mapping exists,
        this handler classifies + logs only — it does NOT open a PR yet,
        so the Gmail-label-on-source-email won't fire either.

        v0.1: once the mapping is wired (env var BUGSNAG_PROJECT_REPOS
        as JSON, or a config file), call FixAgent.run_simple() with the
        parsed error_class as the issue_description and the mapped repo.
        Then return the PR URL so the dispatcher labels the source email.
        """
        # Try to extract the project name (typical subject:
        # '[Bugsnag] Error in MyProject - SomeException: message')
        project_match = re.search(r"error in ([^-\n]+?)(?: - |$)", subject, re.IGNORECASE)
        project = project_match.group(1).strip() if project_match else "unknown"

        # Error class (first word after the project, often before ':')
        error_match = re.search(r"([A-Z][A-Za-z0-9_.]+(?:Error|Exception))", subject)
        error_class = error_match.group(1) if error_match else "unknown"

        logger.info(
            "Bugsnag error: project=%s error_class=%s subject=%r",
            project, error_class, subject[:120],
        )
        # No PR opened yet — return None so no Gmail label is applied.
        # Once project->repo mapping ships, return the fix-PR URL here.
        return None
