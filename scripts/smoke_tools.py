#!/usr/bin/env python3
"""Tool smoke tests — catches the next paramiko-shaped bug before it ships.

Imports every tool module and exercises the ones whose call paths are safe
to invoke without side effects (no PR creation, no SSH, no destructive
writes). Fails on any ImportError, attribute lookup miss, or schema-shape
problem. Run from repo root via `python -m scripts.smoke_tools` or
directly via `python scripts/smoke_tools.py`.

Designed to be cheap (<5s, no network beyond GitHub read) so it can run
on every PR via GitHub Actions.
"""
from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Tool modules whose IMPORT alone is the smoke test. Calling them is either
# destructive (SSH, PR creation, identity registration) or needs runtime
# inputs we don't have in CI (uploaded images, signed payloads). The
# import-only test is still high-value: it catches missing imports
# (paramiko bug from 2026-05-08), syntax errors, and broken refs.
IMPORT_ONLY = [
    "app.tools.deploy",
    "app.tools.dao_identity",
    "app.tools.dao_submission",
    "app.tools.qr_scanner",
    "app.tools.upload_file_to_github",
    "app.tools.inventory_lookup",
]

# Tool modules with safe call paths we exercise to confirm shape + behavior.
CALL_TESTS: list[tuple[str, str, callable, dict]] = []


def _build_call_tests():
    """Lazily build call tests so import failures above are caught first."""
    import os as _os
    from app.tools import fs_tools, github_tools  # noqa: F401

    has_gh_token = bool(_os.getenv("TRUESIGHT_DAO_AUTOPILOT") or _os.getenv("GITHUB_TOKEN"))

    def fs_list_repo_root() -> dict:
        out = fs_tools.list_directory(str(REPO_ROOT))
        assert "files" in out or "entries" in out or "items" in out, f"list_directory unexpected shape: {list(out.keys())}"
        # tolerate any of the common naming conventions
        entries = out.get("files") or out.get("entries") or out.get("items") or []
        names = {e.get("name") for e in entries if isinstance(e, dict)}
        assert "app" in names, f"expected 'app' in repo root listing, got {sorted(names)[:10]}"
        return {"ok": True, "n_entries": len(entries)}

    def gh_read_known_file() -> dict:
        out = github_tools.read_repo_file("agentic_ai_context", "README.md")
        assert isinstance(out, dict), f"read_repo_file returned non-dict: {type(out)}"
        # Either a successful read with 'content', or an error key — both are valid shapes
        assert "content" in out or "error" in out, f"unexpected shape: {list(out.keys())}"
        return {"ok": True, "shape": list(out.keys())}

    tests = [
        ("app.tools.fs_tools.list_directory(repo_root)", "fs_tools", fs_list_repo_root, {}),
    ]
    if has_gh_token:
        tests.append(
            ("app.tools.github_tools.read_repo_file(agentic_ai_context, README.md)", "github_tools", gh_read_known_file, {})
        )
    return tests


def _check_llm_schemas() -> dict:
    """Confirm get_tool_schemas() returns valid JSON-serializable schemas."""
    from app.llm_client import get_tool_schemas

    schemas = get_tool_schemas()
    assert isinstance(schemas, list), f"expected list, got {type(schemas)}"
    assert len(schemas) > 0, "no tool schemas returned"

    seen_names = set()
    for s in schemas:
        try:
            json.dumps(s)
        except TypeError as e:
            raise AssertionError(f"schema not JSON-serializable: {s.get('function', {}).get('name', '?')} — {e}")
        name = s.get("function", {}).get("name")
        assert name, f"schema missing function.name: {s}"
        assert name not in seen_names, f"duplicate schema name: {name}"
        seen_names.add(name)
    return {"ok": True, "n_schemas": len(schemas), "names_sample": sorted(seen_names)[:5]}


def _check_app_routes() -> dict:
    """Confirm key endpoints are wired in the FastAPI app."""
    from app import main as _main

    expected_routes = {"/health", "/chat", "/chat/queue", "/chat/active/{session_short}", "/sessions/new"}
    have = {r.path for r in _main.app.routes if hasattr(r, "path")}
    missing = expected_routes - have
    assert not missing, f"missing routes: {missing}"
    return {"ok": True, "n_routes": len(have)}


def main() -> int:
    failures: list[str] = []
    passes: list[str] = []

    # 1. Import-only tests
    for mod_path in IMPORT_ONLY:
        try:
            importlib.import_module(mod_path)
            passes.append(f"import: {mod_path}")
        except Exception as e:
            failures.append(f"import: {mod_path} — {type(e).__name__}: {e}\n{traceback.format_exc()}")

    # 2. Call tests (guarded — if the lazy build itself fails, that's a failure)
    try:
        for label, mod_short, fn, _kwargs in _build_call_tests():
            try:
                result = fn()
                passes.append(f"call: {label} — {result}")
            except Exception as e:
                failures.append(f"call: {label} — {type(e).__name__}: {e}\n{traceback.format_exc()}")
    except Exception as e:
        failures.append(f"call test setup failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    # 3. LLM schema check
    try:
        result = _check_llm_schemas()
        passes.append(f"llm_schemas: {result}")
    except Exception as e:
        failures.append(f"llm_schemas: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    # 4. FastAPI route check
    try:
        result = _check_app_routes()
        passes.append(f"app_routes: {result}")
    except Exception as e:
        failures.append(f"app_routes: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    print(f"\nSmoke tests: {len(passes)} passed, {len(failures)} failed.\n")
    for p in passes:
        print(f"  PASS  {p}")
    for f in failures:
        print(f"  FAIL  {f}")
        print()

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
