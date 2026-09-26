"""Regression tests on role-tool gating.

Past incidents: the infrastructure role exposed `open_fix_pr` but not
`merge_pr`, so when a governor told autopilot to merge a PR the model had no
way to do it and hallucinated excuses ("no github token on this server").
"""

from __future__ import annotations

from app.roles import ROLES, get_tool_schemas_for_role


def _tool_names(role) -> set[str]:
    return {t["function"]["name"] for t in get_tool_schemas_for_role(role)}


def test_infrastructure_has_merge_pr():
    names = _tool_names(ROLES["infrastructure"])
    assert "merge_pr" in names, "infrastructure role must expose merge_pr"
    assert "open_fix_pr" in names
    assert "deploy_autopilot" in names


def test_infrastructure_has_aws_query():
    names = _tool_names(ROLES["infrastructure"])
    assert "aws_query" in names
    assert "read_google_sheet" in names


def test_general_role_gets_all_tools():
    # Empty tools list = all tools (see get_tool_schemas_for_role).
    names = _tool_names(ROLES["general"])
    for required in (
        "merge_pr",
        "open_fix_pr",
        "aws_query",
        "read_google_sheet",
        "gmail_search",
        "gmail_send",
        "generate_pdf",
    ):
        assert required in names, f"general role should expose {required}"


def test_every_non_empty_role_has_google_sheet_access():
    """Bootstrap rule from 2026-05-28: read_google_sheet goes to every role."""
    for key, role in ROLES.items():
        if not role.tools:  # general — gets all
            continue
        names = _tool_names(role)
        assert "read_google_sheet" in names, f"{key} missing read_google_sheet"
        assert "http_fetch" in names, f"{key} missing http_fetch"


def test_every_non_empty_role_has_gmail_search():
    for key, role in ROLES.items():
        if not role.tools:
            continue
        names = _tool_names(role)
        assert "gmail_search" in names, f"{key} missing gmail_search"
