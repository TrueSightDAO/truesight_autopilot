"""Role registry for topic-aware autopilot personas.

Each Telegram topic gets a role that scopes the available tools and system prompt.
When a new topic is detected (empty session), autopilot asks the user to choose a role.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ROLE_SELECTION_MESSAGE = """👋 **New topic detected!** Before we start, pick my role:

**1.** Content Marketing Researcher — market analysis, SEO, content strategy
**2.** Event Coordinator — plan DAO events and logistics
**3.** SRE / DevOps Engineer — fix bugs, deploy code, monitor infra
**4.** Retailer Outreach Coordinator — partner outreach, onboarding, followups
**5.** Logistics Analyst — import/export, supply chain, freight
**6.** Inventory Manager — QR codes, stock levels, inventory movements
**7.** General DAO Assistant — everything (all tools, no specialisation)

Reply with a number (1-7) or role name. I'll remember this for this topic."""


@dataclass
class Role:
    key: str           # e.g. "content_marketing"
    name: str          # display name
    description: str   # one-liner for the menu
    system_prompt: str  # role-specific system prompt (overrides default)
    tools: list[str]    # allowed tool names (empty = all)
    crewai_enabled: bool = False  # can use CrewAI for autonomous research

    @property
    def menu_line(self) -> str:
        return f"**{self.name}** — {self.description}"


ROLES: dict[str, Role] = {
    "content_marketing": Role(
        key="content_marketing",
        name="Content Marketing Researcher",
        description="market analysis, SEO, content strategy, competitor research",
        system_prompt="""You are a Content Marketing Researcher for Agroverse and TrueSight DAO.
Your job is exhaustive market research, competitive analysis, and content strategy.

## RULES
1. When asked to research a topic, use web_search and web_extract extensively to gather data.
2. Synthesize findings into structured reports with sections: Market Overview, Demographics, Competitors, Trends, Recommendations.
3. File reports in the go_to_market repo using upload_file_to_github.
4. Reference CMO_SETH_GODIN.md principles for marketing strategy.
5. Be thorough — don't stop until you've covered the topic from multiple angles.
6. When in doubt, search again with refined queries.

## WORKFLOW
1. Understand the research topic → 2. Plan search queries → 3. Execute searches → 4. Read top results → 5. Synthesize → 6. Write report → 7. File in repo""",
        tools=["web_search", "web_extract", "read_context_file", "read_repo_file",
               "read_local_file", "list_directory", "upload_file_to_github",
               "list_org_repos", "list_prs",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "aws_query", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
        crewai_enabled=True,
    ),
    "events": Role(
        key="events",
        name="Event Coordinator",
        description="plan DAO events, logistics, scheduling",
        system_prompt="""You are an Event Coordinator for TrueSight DAO and Agroverse.
Your job is planning events, coordinating logistics, and managing schedules.

## TOOLS
- Use web_search for venue research, vendor pricing, event best practices
- Use create_dao_submission to record event-related contributions
- Use read_context_file for DAO governance docs""",
        tools=["web_search", "web_extract", "read_context_file", "read_repo_file",
               "read_local_file", "create_dao_submission", "list_prs",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
    ),
    "infrastructure": Role(
        key="infrastructure",
        name="SRE / DevOps Engineer",
        description="fix bugs, deploy code, monitor AWS infrastructure",
        system_prompt="""You are an SRE / DevOps Engineer for TrueSight DAO.
Your job is debugging production issues, deploying fixes, and monitoring infrastructure.

## RULES
1. Diagnose issues by reading logs, context files, and repo code.
2. Open fix PRs with open_fix_pr — always explain the root cause.
3. Merge a PR only when a governor explicitly tells you to (use merge_pr).
   Never auto-merge on your own. When asked, do not invent excuses about
   missing tokens or scopes — the github PAT is configured on this server
   and the merge_pr tool is available to you. Just call it.
4. Deploy changes via deploy_autopilot when approved.
5. Monitor AWS resources and alert on anomalies.""",
        tools=["open_fix_pr", "merge_pr", "mark_pr_ready_for_review",
               "deploy_autopilot", "read_repo_file",
               "read_context_file", "read_local_file", "list_directory",
               "list_org_repos", "list_prs", "scan_qr_from_file", "web_search",
               "upload_file_to_github",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "aws_query", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
    ),
    "retailer_outreach": Role(
        key="retailer_outreach",
        name="Retailer Outreach Coordinator",
        description="partner outreach, onboarding, followup emails",
        system_prompt="""You are a Retailer Outreach Coordinator for Agroverse.
Your job is managing the holistic wellness retail partner pipeline.

## WORKFLOW
1. Read RETAILER_ONBOARDING_PLAYBOOK.md for the outreach protocol
2. Read HIT_LIST_STATE_MACHINE.md for lead tracking states
3. Use web_search to research potential partners
4. Use create_dao_submission for partner-related events
5. Read PARTNER_OUTREACH_PROTOCOL.md for communication templates""",
        tools=["web_search", "web_extract", "read_context_file", "read_repo_file",
               "read_local_file", "create_dao_submission", "list_prs",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
        crewai_enabled=True,
    ),
    "logistics": Role(
        key="logistics",
        name="Logistics Analyst",
        description="import/export, supply chain, freight, customs",
        system_prompt="""You are a Logistics Analyst for Agroverse.
Your job is analyzing supply chain operations, freight costs, and import/export logistics.

## WORKFLOW
1. Read SUPPLY_CHAIN_AND_FREIGHTING.md for current flows and costs
2. Read CONSIGNMENT_OPTIMAL_QUANTITY_PROPOSAL.md for inventory math
3. Use web_search for freight rates, customs info, shipping timelines
4. Use lookup_qr_code for tracking shipments via QR codes""",
        tools=["web_search", "web_extract", "read_context_file", "read_repo_file",
               "read_local_file", "lookup_qr_code", "lookup_qr_batch", "list_prs",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "aws_query", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
    ),
    "inventory": Role(
        key="inventory",
        name="Inventory Manager",
        description="QR codes, stock levels, inventory movements, restocking",
        system_prompt="""You are an Inventory Manager for Agroverse.
Your job is tracking cacao bag inventory, QR code operations, and stock management.

## WORKFLOW
1. Read AGROVERSE_QR_CODE_BATCH_GENERATION.md for QR code conventions
2. Read RESTOCK_RECOMMENDER_ON_THE_FLY.md for restocking logic
3. Use scan_qr_from_file / scan_qr_batch for uploaded QR code images
4. Use lookup_qr_code / lookup_qr_batch to resolve codes against the ledger
5. Use submit_contribution for inventory movement events
6. Read LEDGER_CONVERSION_AND_REPACKAGING.md for repackaging rules""",
        tools=["scan_qr_from_file", "scan_qr_batch", "lookup_qr_code", "lookup_qr_batch",
               "submit_contribution", "read_context_file", "read_repo_file",
               "read_local_file", "list_directory", "list_prs",
               "read_google_sheet", "read_google_doc", "read_drive_file",
               "list_drive_folder", "http_fetch", "generate_pdf",
               "gmail_search", "gmail_read_message", "gmail_send",
               "gmail_create_draft", "gmail_list_labels", "gmail_apply_label",
               "upload_local_file_to_github"],
    ),
    "general": Role(
        key="general",
        name="General DAO Assistant",
        description="everything — all tools, no specialisation",
        system_prompt="",  # uses default system prompt
        tools=[],  # empty = all tools
    ),
}


def _validate_role_tool_names() -> None:
    """Fatal-on-startup check: every role's ``tools`` list references a tool
    name that exists in the capability manifest. Catches the kind of typo /
    role-drift that caused the 2026-05-28 ``merge_pr`` gating bug.
    """
    from .tool_registry import validate_role_tool_names
    role_tools = {key: list(role.tools) for key, role in ROLES.items() if role.tools}
    errors = validate_role_tool_names(role_tools)
    if errors:
        raise RuntimeError(
            "Role-tool validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            + "\nFix the tool name or add a TOOL_SPEC under app/tools/."
        )


_validate_role_tool_names()


ROLE_ALIASES: dict[str, str] = {
    "1": "content_marketing", "content marketing": "content_marketing",
    "content": "content_marketing", "marketing": "content_marketing",
    "2": "events", "event": "events", "events": "events",
    "3": "infrastructure", "infra": "infrastructure", "devops": "infrastructure",
    "sre": "infrastructure", "digital infrastructure": "infrastructure",
    "4": "retailer_outreach", "outreach": "retailer_outreach",
    "retailer": "retailer_outreach", "retail": "retailer_outreach",
    "5": "logistics", "import": "logistics", "export": "logistics",
    "freight": "logistics", "supply chain": "logistics",
    "6": "inventory", "inventory management": "inventory", "stock": "inventory",
    "7": "general", "default": "general",
}


def resolve_role(user_text: str) -> Role | None:
    """Try to parse a role choice from user text. Returns None if no match."""
    clean = user_text.strip().lower().rstrip(".")
    if clean in ROLE_ALIASES:
        return ROLES[ROLE_ALIASES[clean]]
    for key, role in ROLES.items():
        if role.name.lower() == clean:
            return role
    return None


def get_tool_schemas_for_role(role: Role | None) -> list[dict[str, Any]]:
    """Return filtered tool schemas for a role. None/empty tools = all tools."""
    from .llm_client import get_tool_schemas as _all_schemas
    all_schemas = _all_schemas()
    if role is None or not role.tools:
        return all_schemas
    allowed = set(role.tools)
    return [t for t in all_schemas if t["function"]["name"] in allowed]


def get_system_prompt_for_role(role: Role | None) -> str:
    """Return the system prompt for a role. Falls back to the default."""
    if role and role.system_prompt:
        return role.system_prompt
    from .context import get_system_prompt as _default
    return _default()


ROLE_SYSTEM_TAG = "[ROLE:"
"""Prefix to identify role tags in session history."""


def role_to_tag(role: Role) -> str:
    return f"{ROLE_SYSTEM_TAG} {role.key}]"


def tag_to_role(tag: str) -> Role | None:
    """Extract role from a tag string like '[ROLE: content_marketing]'."""
    if not tag.startswith(ROLE_SYSTEM_TAG):
        return None
    key = tag[len(ROLE_SYSTEM_TAG):].strip().rstrip("]")
    return ROLES.get(key)


def find_role_in_history(history: list[dict]) -> Role | None:
    """Scan session history for a role tag. Returns the role if found."""
    for msg in history:
        content = msg.get("content", "")
        if isinstance(content, str) and content.startswith(ROLE_SYSTEM_TAG):
            return tag_to_role(content)
    return None


def set_role_in_history(history: list[dict], role: Role) -> None:
    """Inject a role tag as the first system message in history."""
    tag = role_to_tag(role)
    history.insert(0, {"role": "system", "content": tag})


def build_role_menu() -> list[dict[str, str]]:
    """Return the role selection message as a list for chat history injection."""
    return [
        {"role": "assistant", "content": ROLE_SELECTION_MESSAGE},
    ]


# ── Context reset for old sessions ─────────────────────────────────────────

RESET_CONTEXT_THRESHOLD = 20
"""Sessions with more than this many messages trigger a 'keep or reset?' prompt when
a role is first set. This prevents 50+ message legacy sessions from drowning the
LLM in stale context."""

PENDING_ROLE_TAG = "[PENDING_ROLE:"
"""Tag to mark a role choice that's awaiting context decision."""


def pending_role_tag(role: Role) -> str:
    return f"{PENDING_ROLE_TAG} {role.key}]"


def tag_to_pending_role(tag: str) -> Role | None:
    if not tag.startswith(PENDING_ROLE_TAG):
        return None
    key = tag[len(PENDING_ROLE_TAG):].strip().rstrip("]")
    return ROLES.get(key)


def find_pending_role(history: list[dict]) -> Role | None:
    """Scan history for a pending role tag. Returns role if found."""
    for msg in history:
        content = msg.get("content", "")
        if isinstance(content, str) and content.startswith(PENDING_ROLE_TAG):
            return tag_to_pending_role(content)
    return None


def reset_context_prompt(role: Role, msg_count: int) -> str:
    return (
        f"This topic has **{msg_count} messages** from before role selection. "
        f"Keeping them would make every response very slow and expensive.\n\n"
        f"**Keep** existing context, or **reset** to start fresh for {role.name}?\n"
        f"Reply `keep` or `reset`."
    )


def archive_old_history(history: list[dict], role: Role) -> list[dict]:
    """Remove all messages except those needed for the role setup.
    Returns a fresh history list with just the role tag."""
    return [{"role": "system", "content": role_to_tag(role)}]
