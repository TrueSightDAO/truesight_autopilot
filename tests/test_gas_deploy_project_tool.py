"""Tests for the gas_deploy_project autopilot tool (subprocess mocked)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from app.tools import gas_deploy_project as gdp


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("GAS_DEPLOY_TOKENOMICS_ROOT", raising=False)


def test_missing_script_id():
    out = json.loads(gdp.gas_deploy_project(""))
    assert out["status"] == "error"
    assert "script_id is required" in out["reason"]


def test_no_tokenomics_checkout(monkeypatch):
    # No env override, default path doesn't exist on the test host.
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: None)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake"))
    assert out["status"] == "error"
    assert "tokenomics checkout not found" in out["reason"]
    assert "GAS_DEPLOY_TOKENOMICS_ROOT" in out["fix"]


def test_dry_run_command_shape(monkeypatch, tmp_path):
    # Make a fake tokenomics checkout.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok dry-run", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake"))
    assert out["status"] == "ok"
    assert "--push" not in captured["cmd"]      # dry-run by default
    assert out["push"] is False
    assert out["with_hooks"] is False
    assert "1Dj3-fake" in captured["cmd"]


def test_push_without_hooks_passes_no_hooks_flag(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="pushed", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake", push=True))
    assert out["status"] == "ok"
    assert "--push" in captured["cmd"]
    assert "--no-hooks" in captured["cmd"]
    assert "--with-hooks" not in captured["cmd"]
    assert out["with_hooks"] is False


def test_push_with_hooks_passes_with_hooks_flag(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="pushed + hooks", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake", push=True, with_hooks=True))
    assert out["status"] == "ok"
    assert "--push" in captured["cmd"]
    assert "--with-hooks" in captured["cmd"]
    assert "--no-hooks" not in captured["cmd"]
    assert out["with_hooks"] is True


def test_with_hooks_without_push_is_ignored(monkeypatch, tmp_path):
    # Calling with `with_hooks=True` but `push=False` should still dry-run.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="dry", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake", push=False, with_hooks=True))
    assert out["status"] == "ok"
    assert "--push" not in captured["cmd"]
    assert "--with-hooks" not in captured["cmd"]
    # Tool result still reflects what the model asked vs what fired.
    assert out["push"] is False
    assert out["with_hooks"] is False


def test_nonzero_exit_returns_error(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake"))
    assert out["status"] == "error"
    assert out["exit_code"] == 2
    assert "boom" in out["stderr"]


def test_timeout_returns_error(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0), output="partial")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake", timeout_secs=1))
    assert out["status"] == "error"
    assert "timed out" in out["reason"]


def test_output_truncation(monkeypatch, tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "deploy_gas_project.py").write_text("# fake")
    monkeypatch.setattr(gdp, "_resolve_tokenomics_root", lambda: tmp_path)
    huge = "x" * (gdp._MAX_OUTPUT_CHARS + 1000)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=huge, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = json.loads(gdp.gas_deploy_project("1Dj3-fake"))
    assert out["status"] == "ok"
    assert out["stdout_truncated"] is True
    assert len(out["stdout"]) <= gdp._MAX_OUTPUT_CHARS


def test_tool_spec_in_registry():
    """Regression: the tool is auto-discovered by the capability manifest."""
    from app.tool_registry import discover_tools
    names = {s.name for s in discover_tools()}
    assert "gas_deploy_project" in names


def test_tool_gated_on_infrastructure_role():
    """Regression: SRE role has access (it's a deploy tool)."""
    from app.roles import ROLES, get_tool_schemas_for_role
    infra_names = {t["function"]["name"] for t in get_tool_schemas_for_role(ROLES["infrastructure"])}
    assert "gas_deploy_project" in infra_names
