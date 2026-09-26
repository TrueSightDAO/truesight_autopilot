"""Unit tests for app/tools/agent_handoff.py (send_handoff, check_handoffs)."""

from __future__ import annotations

import json

from app.config import Settings, settings
from app.tools import agent_handoff


def test_agent_name_defaults_to_sophia():
    assert Settings().agent_name == "sophia"


def test_agent_name_can_be_overridden():
    assert Settings(AGENT_NAME="bionpact").agent_name == "bionpact"


def test_agent_handoffs_repo_is_api_only_by_default():
    assert "agent_handoffs" in Settings().api_only_repos


def test_send_handoff_requires_target_and_summary():
    result = json.loads(agent_handoff.send_handoff("", "a summary"))
    assert result["status"] == "error"
    result2 = json.loads(agent_handoff.send_handoff("bionpact", ""))
    assert result2["status"] == "error"


def test_send_handoff_rejects_self_target(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "sophia")
    result = json.loads(agent_handoff.send_handoff("sophia", "a summary"))
    assert result["status"] == "error"
    assert "itself" in result["reason"]


def test_send_handoff_writes_correctly_named_file(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "sophia")
    monkeypatch.setattr(agent_handoff, "_registry_lookup", lambda name: {"name": name})

    captured = {}

    def fake_upload(repo, path, content, message):
        captured["repo"] = repo
        captured["path"] = path
        captured["content"] = content
        return {"status": "success", "content_url": "https://example.com/x"}

    monkeypatch.setattr(agent_handoff, "upload_file_to_github", fake_upload)

    result = json.loads(
        agent_handoff.send_handoff(
            "bionpact", "test summary", "test context", "thread-42"
        )
    )
    assert result["status"] == "success"
    assert captured["repo"] == "agent_handoffs"
    assert captured["path"].startswith("handoffs/bionpact_from_sophia_")
    assert captured["path"].endswith(".json")

    payload = json.loads(captured["content"])
    assert payload["from"] == "sophia"
    assert payload["to"] == "bionpact"
    assert payload["summary"] == "test summary"
    assert payload["context"] == "test context"
    assert payload["thread_id"] == "thread-42"


def test_send_handoff_survives_registry_lookup_failure(monkeypatch):
    """A typo'd or not-yet-registered target must not hard-block the handoff
    (best-effort validation, not a gate) — matches the docstring contract."""
    monkeypatch.setattr(settings, "agent_name", "sophia")
    monkeypatch.setattr(agent_handoff, "_registry_lookup", lambda name: None)
    monkeypatch.setattr(
        agent_handoff,
        "upload_file_to_github",
        lambda **kwargs: {"status": "success", "content_url": "x"},
    )
    result = json.loads(agent_handoff.send_handoff("unknown_agent", "hi"))
    assert result["status"] == "success"


def test_send_handoff_surfaces_upload_error(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "sophia")
    monkeypatch.setattr(agent_handoff, "_registry_lookup", lambda name: {"name": name})
    monkeypatch.setattr(
        agent_handoff,
        "upload_file_to_github",
        lambda **kwargs: {"status": "error", "error": "403 Forbidden"},
    )
    result = json.loads(agent_handoff.send_handoff("bionpact", "hi"))
    assert result["status"] == "error"


def test_check_handoffs_filters_by_own_name_prefix(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "bionpact")

    def fake_read_repo_file(repo, path, ref="main"):
        if path == "handoffs":
            return {
                "type": "directory",
                "entries": [
                    {
                        "name": "bionpact_from_sophia_20260819T000000Z.json",
                        "path": "handoffs/bionpact_from_sophia_20260819T000000Z.json",
                        "type": "file",
                    },
                    {
                        "name": "sophia_from_bionpact_20260818T000000Z.json",
                        "path": "handoffs/sophia_from_bionpact_20260818T000000Z.json",
                        "type": "file",
                    },
                    {"name": "README.md", "path": "handoffs/README.md", "type": "file"},
                ],
            }
        assert path == "handoffs/bionpact_from_sophia_20260819T000000Z.json"
        return {
            "type": "file",
            "content": json.dumps(
                {
                    "from": "sophia",
                    "to": "bionpact",
                    "timestamp": "2026-08-19T00:00:00Z",
                    "summary": "hi",
                }
            ),
        }

    monkeypatch.setattr(agent_handoff, "read_repo_file", fake_read_repo_file)

    result = json.loads(agent_handoff.check_handoffs())
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["handoffs"][0]["from"] == "sophia"


def test_check_handoffs_empty_when_no_matches(monkeypatch):
    monkeypatch.setattr(settings, "agent_name", "sophia")
    monkeypatch.setattr(
        agent_handoff,
        "read_repo_file",
        lambda repo, path, ref="main": {
            "type": "directory",
            "entries": [
                {"name": "README.md", "path": "handoffs/README.md", "type": "file"}
            ],
        },
    )
    result = json.loads(agent_handoff.check_handoffs())
    assert result["status"] == "success"
    assert result["count"] == 0


def test_check_handoffs_handles_list_error(monkeypatch):
    monkeypatch.setattr(
        agent_handoff,
        "read_repo_file",
        lambda repo, path, ref="main": {"type": "error", "error": "404"},
    )
    result = json.loads(agent_handoff.check_handoffs())
    assert result["status"] == "error"


def test_tool_specs_registered():
    names = {spec.name for spec in agent_handoff.TOOL_SPECS}
    assert names == {"send_handoff", "check_handoffs"}
