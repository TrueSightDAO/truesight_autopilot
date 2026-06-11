"""Tests for the capability manifest / tool registry."""

from __future__ import annotations

import json

from app.tool_registry import (
    ToolSpec,
    discover_tools,
    dispatch,
    get_registry,
    get_tool_names,
    reset_registry_for_tests,
    validate_role_tool_names,
)


def test_discover_finds_known_tools():
    names = {s.name for s in discover_tools()}
    # Sanity-check a representative slice across modules.
    for tool in (
        "read_google_sheet",
        "read_google_doc",
        "read_drive_file",
        "list_drive_folder",
        "http_fetch",
        "aws_query",
        "generate_pdf",
        "gmail_search",
        "gmail_send",
        "merge_pr",
        "mark_pr_ready_for_review",
        "upload_file_to_github",
        "upload_local_file_to_github",
        "list_org_repos",
        "list_prs",
        "read_repo_file",
        "read_context_file",
        "scan_qr_from_file",
        "lookup_qr_code",
        "register_identity",
        "web_search",
        "web_extract",
        "list_directory",
        "read_local_file",
        "read_oracle_logs",
        "deploy_autopilot",
        # Orchestration tools that have schemas in the manifest but stay
        # inline-dispatched:
        "submit_contribution",
        "create_dao_submission",
        "open_fix_pr",
    ):
        assert tool in names, f"manifest missing {tool}"


def test_orchestration_tools_have_handler_none():
    by_name = {s.name: s for s in discover_tools()}
    for tool in ("submit_contribution", "create_dao_submission", "open_fix_pr"):
        assert by_name[tool].handler is None, f"{tool} should be inline-dispatched"


def test_simple_tools_have_handler_set():
    by_name = {s.name: s for s in discover_tools()}
    for tool in (
        "read_google_sheet",
        "gmail_search",
        "aws_query",
        "http_fetch",
        "generate_pdf",
        "merge_pr",
        "mark_pr_ready_for_review",
        "upload_local_file_to_github",
    ):
        assert by_name[tool].handler is not None, f"{tool} should have a handler"


def test_get_tool_schemas_matches_registry():
    """The legacy entrypoint must return the same name set as the registry."""
    from app.llm_client import get_tool_schemas

    schema_names = {t["function"]["name"] for t in get_tool_schemas()}
    registry_names = get_tool_names()
    assert schema_names == registry_names


def test_dispatch_returns_none_for_unknown_tool():
    assert dispatch("not_a_real_tool", {}, {}) is None


def test_dispatch_returns_none_for_inline_orchestration():
    # Orchestration tools are still inline in main.py.
    assert dispatch("submit_contribution", {"event_name": "x", "attributes": {}}, {}) is None
    assert dispatch("open_fix_pr", {"repo": "x", "issue_description": "y"}, {}) is None


def test_dispatch_executes_handler():
    # Use a simple text-only tool with no external deps.
    out = dispatch("list_directory", {"dir_path": "/nonexistent_path_for_testing"}, {})
    assert out is not None
    parsed = json.loads(out)
    # list_directory returns an error shape for missing paths — confirms dispatch ran.
    assert parsed.get("status") == "error"
    assert "not found" in parsed.get("message", "").lower()


def test_handler_exception_returns_error_json():
    # Inject a misbehaving spec, exercise dispatch.
    reset_registry_for_tests()
    spec = ToolSpec(
        name="__test_boom",
        description="raises",
        parameters={"type": "object", "properties": {}},
        handler=lambda args, ctx: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    registry = get_registry()
    registry[spec.name] = spec
    try:
        out = dispatch("__test_boom", {}, {})
        parsed = json.loads(out)
        assert parsed["status"] == "error"
        assert "boom" in parsed["reason"]
    finally:
        registry.pop(spec.name, None)
        reset_registry_for_tests()


def test_validate_role_tool_names_catches_typo():
    errors = validate_role_tool_names({"infrastructure": ["mrge_pr"]})
    assert errors and "mrge_pr" in errors[0]


def test_validate_role_tool_names_passes_real_roles():
    from app.roles import ROLES

    role_tools = {key: list(role.tools) for key, role in ROLES.items() if role.tools}
    errors = validate_role_tool_names(role_tools)
    assert errors == [], f"role validation failed: {errors}"
