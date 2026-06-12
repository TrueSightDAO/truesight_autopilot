"""Poll Gmail for actionable emails: GitHub failures, GAS errors, alerts."""

from __future__ import annotations

import base64
import json
import logging

from .deploy_watcher import heartbeat as _track_heartbeat, register_track as _register_track, unregister_track as _unregister_track
import re
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import settings
from .fix_agent import FixAgent
from .github_client import GitHubClient
from .llm_client import LLMClient

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
            logger.error(
                "Failed to build Gmail service: %s — email polling disabled", e
            )
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
        results = (
            self.gmail.users()
            .messages()
            .list(userId="me", q="is:unread", maxResults=20)
            .execute()
        )
        messages = results.get("messages", [])
        processed = 0

        for msg_meta in messages:
            msg_id = msg_meta["id"]
            # users.messages.get does NOT modify read state (only modify/UI does),
            # so fetching here is safe — the message stays UNREAD until we decide.
            msg = self.gmail.users().messages().get(userId="me", id=msg_id).execute()
            payload = msg.get("payload", {})
            headers = {
                h["name"].lower(): h["value"] for h in payload.get("headers", [])
            }

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
            logger.warning(
                "Could not apply Gmail label %r to message %s: %s",
                label_name,
                msg_id,
                e,
            )

    def _get_or_create_label(self, name: str) -> str | None:
        """Return Gmail label ID for `name`, creating it if missing."""
        try:
            existing = self.gmail.users().labels().list(userId="me").execute()
            for lbl in existing.get("labels", []):
                if lbl.get("name") == name:
                    return lbl.get("id")
            created = (
                self.gmail.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
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

    # Dedup state — avoid re-triaging the same Bugsnag error_id every time
    # Bugsnag re-emails about it (re-occurrences, regression alerts, etc.).
    # JSON file at /opt/truesight_autopilot/state/bugsnag_triaged_errors.json,
    # structured as { error_id: {pr_url, triaged_at_utc, project} }.
    BUGSNAG_DEDUP_PATH = Path(
        "/opt/truesight_autopilot/state/bugsnag_triaged_errors.json"
    )

    def _load_bugsnag_dedup(self) -> dict:
        try:
            return json.loads(self.BUGSNAG_DEDUP_PATH.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("bugsnag dedup file unreadable, starting fresh: %s", e)
            return {}

    def _save_bugsnag_dedup(self, state: dict) -> None:
        try:
            self.BUGSNAG_DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.BUGSNAG_DEDUP_PATH.write_text(
                json.dumps(state, indent=2, sort_keys=True)
            )
        except Exception as e:
            logger.warning("could not persist bugsnag dedup state: %s", e)

    def _handle_bugsnag_error(self, subject: str, body: str) -> str | None:
        """Triage a Bugsnag error notification email and (when mapped) open a fix PR.

        Bugsnag email subjects look like:
          '[<Project Name>] <Error::Class> in <ContextClass>@<Module>'

        Project-name extraction reads the bracketed prefix (was previously
        the 'in <X>' tail, which yielded the context-class instead of the
        project — fixed in v0.1).

        Repo lookup uses the BUGSNAG_PROJECT_REPOS JSON dict from the
        environment. Unmapped projects log a warning and the handler
        returns None — preserves the v0 stub behavior for projects Gary
        hasn't yet vouched for autopilot to fix.

        Dedup (v0.2): each Bugsnag error has a stable error_id embedded
        in the 'errors/<id>' URL inside the email body. Once a fix PR
        is opened, the error_id is recorded in
        /opt/truesight_autopilot/state/bugsnag_triaged_errors.json.
        Subsequent re-emails about the SAME error_id are skipped — this
        prevents re-triage spam when Bugsnag re-fires regression alerts
        or new occurrences of an error already under review.
        """
        # Project name = bracketed prefix in the subject. Falls back to 'unknown'.
        project = "unknown"
        bracket = re.match(r"\s*\[([^\]]+)\]", subject)
        if bracket:
            project = bracket.group(1).strip()

        # Error class (first '<Word>Error' or '<Word>Exception' token).
        error_match = re.search(r"([A-Z][A-Za-z0-9_:]+(?:Error|Exception))", subject)
        error_class = error_match.group(1) if error_match else "unknown"

        # Bugsnag error_id — stable across re-occurrences. Pulled from the
        # 'errors/<hex>' URL pattern Bugsnag puts in every notification body.
        error_id_match = re.search(
            r"app\.bugsnag\.com/[^/\s]+/[^/\s]+/errors/([0-9a-f]+)", body, re.IGNORECASE
        )
        error_id = error_id_match.group(1) if error_id_match else None

        logger.info(
            "Bugsnag error: project=%s error_class=%s error_id=%s subject=%r",
            project,
            error_class,
            error_id,
            subject[:140],
        )

        # Dedup short-circuit
        if error_id:
            triaged = self._load_bugsnag_dedup()
            if error_id in triaged:
                prior = triaged[error_id]
                logger.info(
                    "Bugsnag error_id=%s already triaged %s -> %s; skipping",
                    error_id,
                    prior.get("triaged_at_utc"),
                    prior.get("pr_url"),
                )
                return None

        # Resolve project -> repo via operator-maintained mapping
        repo = None
        raw = (settings.bugsnag_project_repos_raw or "").strip()
        if raw:
            try:
                mapping = json.loads(raw)
                if isinstance(mapping, dict):
                    repo = mapping.get(project)
            except json.JSONDecodeError as e:
                logger.warning("BUGSNAG_PROJECT_REPOS is not valid JSON: %s", e)

        if not repo:
            logger.info(
                "Bugsnag project %r has no repo mapping in BUGSNAG_PROJECT_REPOS; "
                "no fix PR will be opened. Add a mapping to enable autopilot triage.",
                project,
            )
            return None

        if settings.dry_run:
            logger.info(
                "DRY_RUN set — would have run FixAgent on repo=%s for %s",
                repo,
                error_class,
            )
            return None

        # Build a focused issue_description the LLM can act on. The body
        # often contains the stack trace; pass the first ~2000 chars so
        # the agent has the context it needs without inflating tokens.
        body_excerpt = (body or "").strip()
        if len(body_excerpt) > 2000:
            body_excerpt = (
                body_excerpt[:2000] + "\n\n[... truncated for fix-agent context ...]"
            )
        issue_description = (
            f"Bugsnag error in project '{project}': {error_class}\n\n"
            f"Email subject: {subject}\n\n"
            f"Email body excerpt (typically contains stack trace):\n{body_excerpt}"
        )
        logger.info("Bugsnag triage: dispatching FixAgent to repo=%s", repo)
        try:
            agent = FixAgent()
            pr_url = agent.run_simple(repo, issue_description)
            if pr_url:
                logger.info("Bugsnag triage opened fix PR: %s", pr_url)
                # Record the dedup entry so re-emails about this same
                # error_id don't kick off another fix loop.
                if error_id:
                    triaged = self._load_bugsnag_dedup()
                    triaged[error_id] = {
                        "pr_url": pr_url,
                        "triaged_at_utc": datetime.now(timezone.utc).isoformat(),
                        "project": project,
                        "error_class": error_class,
                        "repo": repo,
                    }
                    self._save_bugsnag_dedup(triaged)
                return pr_url
            logger.info("Bugsnag triage: FixAgent did not produce a PR")
        except Exception as e:
            logger.exception("Bugsnag triage failed for project=%s: %s", project, e)
        return None
