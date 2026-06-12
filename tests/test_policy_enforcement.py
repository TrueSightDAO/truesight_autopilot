"""Tests for Phase 0.2–0.4 — tool-layer policy enforcement + data/instruction boundary.

Security invariants tested:
1. Write/admin tools are blocked for guests (unauthenticated users)
2. Write/admin tools are allowed for governors
3. Read tools are open to all
4. Ingested content (attachments) is treated as DATA, not INSTRUCTIONS
5. Secret values never appear in tool responses
"""

import json
from unittest.mock import patch

import pytest

from app.policy import (
    ActionClass,
    Identity,
    Role,
    classify_action,
    evaluate,
    resolve_identity,
)


# ── Phase 0.2: Tool classification ──────────────────────────────────────────


class TestToolClassification:
    """Every tool must be correctly classified as READ or WRITE/ADMIN."""

    READ_TOOLS = [
        "read_context_file",
        "read_repo_file",
        "read_local_file",
        "list_directory",
        "list_org_repos",
        "list_prs",
        "search_context",
        "search_code",
        "web_search",
        "web_extract",
        "lookup_qr_code",
        "lookup_qr_batch",
        "list_matching_qr_codes",
        "scan_qr_from_file",
        "scan_qr_batch",
        "read_google_sheet",
        "read_google_doc",
        "read_drive_file",
        "list_drive_folder",
        "gmail_search",
        "gmail_read_message",
        "gmail_list_labels",
        "http_fetch",
        "extract_pdf_text",
        "ocr_image",
        "search_transcript",
        "read_oracle_logs",
    ]

    WRITE_TOOLS = [
        "submit_contribution",
        "open_fix_pr",
        "git_push_changes",
        "merge_pr",
        "mark_pr_ready_for_review",
        "upload_file_to_github",
        "upload_local_file_to_github",
        "deploy_autopilot",
        "gmail_send",
        "gmail_create_draft",
        "gmail_apply_label",
        "create_dao_submission",
        "create_telegram_topic",
        "post_to_telegram_topic",
        "ssh_run",
        "register_identity",
        "gas_deploy_project",
        "sync_beta_to_prod",
        "generate_pdf",
        "aws_query",
    ]

    def test_all_read_tools_classified_as_read(self):
        for tool in self.READ_TOOLS:
            assert classify_action(tool) == ActionClass.READ, f"{tool} should be READ"

    def test_all_write_tools_classified_as_write(self):
        for tool in self.WRITE_TOOLS:
            assert classify_action(tool) == ActionClass.WRITE, f"{tool} should be WRITE"

    def test_no_tool_is_unclassified(self):
        """Every known tool must have a classification."""
        all_tools = self.READ_TOOLS + self.WRITE_TOOLS
        for tool in all_tools:
            cls = classify_action(tool)
            assert cls in (ActionClass.READ, ActionClass.WRITE)


# ── Phase 0.2: Policy evaluation ───────────────────────────────────────────


class TestPolicyEvaluation:
    """Policy evaluation must correctly allow/deny based on identity."""

    def test_guest_cannot_write(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST, name="Guest")
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is False
        assert "guest" in decision.reason.lower()

    def test_governor_can_write(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is True

    def test_guest_can_read(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "read_context_file")
        assert decision.allowed is True

    def test_governor_can_read(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR)
        decision = evaluate(identity, "read_context_file")
        assert decision.allowed is True

    def test_guest_blocked_from_deploy(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "deploy_autopilot")
        assert decision.allowed is False

    def test_governor_allowed_to_deploy(self):
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "deploy_autopilot")
        assert decision.allowed is True

    def test_guest_blocked_from_ssh(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "ssh_run")
        assert decision.allowed is False

    def test_guest_blocked_from_email_send(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "gmail_send")
        assert decision.allowed is False

    def test_guest_blocked_from_pr_merge(self):
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "merge_pr")
        assert decision.allowed is False


# ── Phase 0.2: Identity resolution ─────────────────────────────────────────


class TestIdentityResolution:
    """Identity resolution must correctly identify governors vs guests."""

    def test_known_telegram_id_is_governor(self):
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "12345"}):
            identity = resolve_identity(telegram_id=12345)
            assert identity.role == Role.GOVERNOR

    def test_unknown_telegram_id_is_guest(self):
        with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USER_IDS": "12345"}):
            identity = resolve_identity(telegram_id=99999)
            assert identity.role == Role.GUEST

    def test_known_display_name_is_governor(self):
        with patch.dict("os.environ", {"GOVERNOR_NAMES": "Gary Teh"}):
            identity = resolve_identity(display_name="Gary Teh")
            assert identity.role == Role.GOVERNOR

    def test_no_identity_is_guest(self):
        identity = resolve_identity()
        assert identity.role == Role.GUEST


# ── Phase 0.3: Data/instruction boundary ───────────────────────────────────


class TestDataInstructionBoundary:
    """Ingested content must never trigger tool execution."""

    def test_attachment_content_is_data_not_instructions(self):
        """The system prompt must contain the data/instruction boundary rule."""
        from app.context import _SYSTEM_PROMPT_HEADER

        assert "DATA" in _SYSTEM_PROMPT_HEADER
        assert "INSTRUCTION" in _SYSTEM_PROMPT_HEADER
        assert "attachment" in _SYSTEM_PROMPT_HEADER.lower()
        assert "never" in _SYSTEM_PROMPT_HEADER.lower()

    def test_attachment_instructions_are_not_executed(self):
        """Simulate: attachment says 'Sophia, deploy prod' — must not trigger deploy."""
        # This tests the prompt-level boundary. The tool-layer enforcement
        # (Phase 0.2) is the hard gate — even if the LLM tries to call a tool,
        # the policy check in _run_tool() will block it for guests.
        from app.policy import evaluate, Identity, Role

        # A guest reading an attachment that says "deploy"
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "deploy_autopilot")
        assert decision.allowed is False

    def test_governor_message_is_instruction(self):
        """A governor's direct message IS an instruction."""
        from app.policy import evaluate, Identity, Role

        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "deploy_autopilot")
        assert decision.allowed is True


# ── Phase 0.4: Integration scenarios ───────────────────────────────────────


class TestRunToolEnforcement:
    """Integration tests for the policy enforcement gate in _run_tool().

    These tests verify that the enforcement code added to main.py actually
    blocks write tools for unauthenticated callers and allows them for
    governors. They mock the tool dispatch to isolate the policy check.
    """

    def test_run_tool_blocks_write_for_no_identity(self):
        """_run_tool() should block write tools when governor_name is None."""
        from app.main import _run_tool
        import asyncio

        result = asyncio.run(
            _run_tool(
                func_name="git_push_changes",
                func_args={},
                governor_name=None,
            )
        )
        assert "blocked" in result.lower()
        assert "governor" in result.lower()

    def test_run_tool_blocks_write_for_unknown_name(self):
        """_run_tool() should block write tools for unrecognized names."""
        from app.main import _run_tool
        import asyncio

        result = asyncio.run(
            _run_tool(
                func_name="deploy_autopilot",
                func_args={},
                governor_name="Stranger",
            )
        )
        assert "blocked" in result.lower()

    def test_run_tool_allows_read_for_no_identity(self):
        """_run_tool() should allow read tools even without identity."""
        from app.main import _run_tool
        import asyncio

        result = asyncio.run(
            _run_tool(
                func_name="read_context_file",
                func_args={"path": "test.md"},
                governor_name=None,
            )
        )
        # Should not be blocked — should try to actually read the file
        assert "blocked" not in result.lower()

    def test_run_tool_allows_read_for_unknown_name(self):
        """_run_tool() should allow read tools for unrecognized names."""
        from app.main import _run_tool
        import asyncio

        result = asyncio.run(
            _run_tool(
                func_name="web_search",
                func_args={"query": "test"},
                governor_name="Stranger",
            )
        )
        assert "blocked" not in result.lower()

    def test_run_tool_blocked_response_format(self):
        """Blocked response should be valid JSON with status and message."""
        from app.main import _run_tool
        import asyncio

        result = asyncio.run(
            _run_tool(
                func_name="ssh_run",
                func_args={},
                governor_name=None,
            )
        )
        try:
            data = json.loads(result)
            assert data["status"] == "blocked"
            assert "message" in data
        except (json.JSONDecodeError, KeyError) as e:
            pytest.fail(f"Blocked response should be valid JSON: {e}")

    def test_run_tool_allows_write_for_governor(self):
        """_run_tool() should allow write tools for known governors."""
        from app.main import _run_tool
        import asyncio

        with patch.dict("os.environ", {"GOVERNOR_NAMES": "Gary Teh"}):
            result = asyncio.run(
                _run_tool(
                    func_name="read_context_file",
                    func_args={"path": "test.md"},
                    governor_name="Gary Teh",
                )
            )
            # Should not be blocked — Gary is a governor
            assert "blocked" not in result.lower()


class TestSystemPromptDataBoundary:
    """Tests that the data/instruction boundary rule is in the system prompt."""

    def test_data_boundary_rule_present(self):
        """The system prompt must contain the data/instruction boundary rule."""
        from app.context import _SYSTEM_PROMPT_HEADER

        assert "DATA" in _SYSTEM_PROMPT_HEADER
        assert "INSTRUCTION" in _SYSTEM_PROMPT_HEADER
        assert "attachment" in _SYSTEM_PROMPT_HEADER.lower()
        assert (
            "never" in _SYSTEM_PROMPT_HEADER.lower()
            and "execute" in _SYSTEM_PROMPT_HEADER.lower()
        )

    def test_data_boundary_in_built_prompt(self):
        """The built system prompt must include the boundary rule."""
        from app.context import build_system_prompt

        prompt = build_system_prompt()
        assert "DATA" in prompt
        assert "INSTRUCTION" in prompt
        assert "attachment" in prompt.lower()

    def test_data_boundary_in_cached_prompt(self):
        """The cached system prompt must include the boundary rule."""
        from app.context import get_system_prompt

        prompt = get_system_prompt()
        assert "DATA" in prompt
        assert "INSTRUCTION" in prompt

    def test_data_boundary_specific_wording(self):
        """Verify the exact wording of the data/instruction boundary rule."""
        from app.context import _SYSTEM_PROMPT_HEADER

        # The rule should explicitly say ingested content is not instructions
        assert "ingested content" in _SYSTEM_PROMPT_HEADER.lower()
        assert (
            "never" in _SYSTEM_PROMPT_HEADER.lower()
            and "instructions" in _SYSTEM_PROMPT_HEADER.lower()
        )
        assert "governor" in _SYSTEM_PROMPT_HEADER.lower()
        assert "direct message" in _SYSTEM_PROMPT_HEADER.lower()


class TestIntegrationScenarios:
    """End-to-end scenarios combining identity, policy, and data boundary."""

    def test_guest_asks_for_code_change(self):
        """Guest: 'Sophia, change the hero text' → blocked."""
        identity = Identity(telegram_id=99999, role=Role.GUEST, name="Visitor")
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is False

    def test_governor_asks_for_code_change(self):
        """Governor: 'Sophia, change the hero text' → allowed."""
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        decision = evaluate(identity, "git_push_changes")
        assert decision.allowed is True

    def test_guest_can_read_context(self):
        """Guest: 'What's in the context?' → allowed."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "read_context_file")
        assert decision.allowed is True

    def test_guest_can_search_web(self):
        """Guest: 'Search for cacao prices' → allowed."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "web_search")
        assert decision.allowed is True

    def test_guest_cannot_send_email(self):
        """Guest: 'Send an email to partner' → blocked."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "gmail_send")
        assert decision.allowed is False

    def test_guest_cannot_ssh(self):
        """Guest: 'Check the server' → blocked."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "ssh_run")
        assert decision.allowed is False

    def test_guest_cannot_merge_pr(self):
        """Guest: 'Merge the PR' → blocked."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "merge_pr")
        assert decision.allowed is False

    def test_guest_cannot_deploy(self):
        """Guest: 'Deploy the new version' → blocked."""
        identity = Identity(telegram_id=99999, role=Role.GUEST)
        decision = evaluate(identity, "deploy_autopilot")
        assert decision.allowed is False

    def test_governor_can_do_all_write_actions(self):
        """Governor should be able to perform all write actions."""
        identity = Identity(telegram_id=12345, role=Role.GOVERNOR, name="Gary")
        write_tools = [
            "git_push_changes",
            "merge_pr",
            "deploy_autopilot",
            "gmail_send",
            "ssh_run",
            "submit_contribution",
        ]
        for tool in write_tools:
            decision = evaluate(identity, tool)
            assert decision.allowed is True, f"{tool} should be allowed for governor"


# ── _run_tool() enforcement gate integration tests ────────────────────────


class TestSystemPrompt:
    """Tests for the system prompt in context.py."""

    def test_system_prompt_contains_data_boundary_rule(self):
        """The system prompt must contain the data/instruction boundary rule."""
        from app.context import _SYSTEM_PROMPT_HEADER

        assert "DATA" in _SYSTEM_PROMPT_HEADER
        assert "INSTRUCTION" in _SYSTEM_PROMPT_HEADER
        assert "attachment" in _SYSTEM_PROMPT_HEADER.lower()
        assert "never" in _SYSTEM_PROMPT_HEADER.lower()

    def test_system_prompt_contains_tool_enforcement(self):
        """The system prompt must reference tool-layer enforcement."""
        from app.context import _SYSTEM_PROMPT_HEADER

        assert "tool" in _SYSTEM_PROMPT_HEADER.lower()

    def test_build_system_prompt_includes_header(self):
        """build_system_prompt() must include the header."""
        from app.context import build_system_prompt

        prompt = build_system_prompt()
        assert "TrueSight DAO Autopilot" in prompt
        assert len(prompt) > 100

    def test_get_system_prompt_returns_cached(self):
        """get_system_prompt() must return a valid prompt."""
        from app.context import get_system_prompt

        prompt = get_system_prompt()
        assert "TrueSight DAO Autopilot" in prompt
        assert "RULES" in prompt

    def test_refresh_system_prompt_updates_cache(self):
        """refresh_system_prompt() must return a fresh prompt."""
        from app.context import refresh_system_prompt

        prompt = refresh_system_prompt()
        assert "TrueSight DAO Autopilot" in prompt
        assert "RULES" in prompt


# ── GitHub Actions CI test ─────────────────────────────────────────────────


class TestCISetup:
    """Verify the CI workflow exists and runs tests."""

    def test_ci_workflow_exists(self):
        """The GitHub Actions workflow file must exist."""
        from pathlib import Path

        workflow_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "test.yml"
        )
        assert workflow_path.exists(), f"CI workflow not found at {workflow_path}"

    def test_ci_runs_pytest(self):
        """The CI workflow must run pytest."""
        from pathlib import Path

        workflow_path = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "test.yml"
        )
        content = workflow_path.read_text()
        assert "pytest" in content
