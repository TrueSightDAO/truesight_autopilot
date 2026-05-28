"""Capability manifest / tool registry.

Single source of truth for what tools the autopilot agent exposes to the LLM.

Each tool module under ``app/tools/`` exports either:

- ``TOOL_SPEC: ToolSpec`` — a single tool, OR
- ``TOOL_SPECS: list[ToolSpec]`` — for modules that bundle multiple operations
  (e.g. ``gmail_tools.py`` exposes six).

Discovery happens at module import time: ``get_registry()`` walks the package,
collects all specs, and returns a name→spec dict. Results are memoised.

The agent's dispatch path:

1. ``llm_client.get_tool_schemas()`` reads ``parameters`` from the registry.
2. ``main._run_tool()`` looks up the spec; if ``handler`` is set, calls it
   with ``(args_dict, context_dict)``; otherwise falls through to the legacy
   inline branch.

Adding a new simple tool:

```python
# app/tools/my_tool.py
from ..tool_registry import ToolSpec

def my_tool(arg: str) -> str:
    return json.dumps({"status": "ok", "echo": arg})

TOOL_SPEC = ToolSpec(
    name="my_tool",
    description="One-line description for the model.",
    parameters={"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]},
    handler=lambda args, ctx: my_tool(args.get("arg", "")),
    default_roles=None,  # None = every non-empty role; or pass a set of role keys
)
```

That's it — no edits to ``llm_client.py`` / ``main.py`` / ``roles.py``.

See ``app/tools/README.md`` for the full authoring guide.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger("autopilot.tool_registry")


# A handler takes (args_dict, context_dict) and returns a string (usually
# JSON-encoded). Async handlers are not supported in this PR — the existing
# dispatcher is sync-friendly. context_dict carries optional session state:
# {"history": list[dict], "session_id": str | None, "governor_name": str | None,
#  "session_key": str | None}.
Handler = Callable[[dict, dict], str]


@dataclass(frozen=True)
class ToolSpec:
    """Declarative tool definition consumed by the registry."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler | None = None
    """``None`` means the legacy inline branch in ``main._run_tool`` handles
    dispatch. Used for the four orchestration tools (submit_contribution,
    open_fix_pr, merge_pr, create_dao_submission) until a future PR migrates
    them. Schema + role-gating still flow through the registry."""

    default_roles: frozenset[str] | None = None
    """Roles that should auto-gain access when ``Role.tools`` does NOT
    explicitly list this tool. ``None`` is the *uniform* default — every
    non-empty role gains access, matching the post-2026-05-28 default for
    google/gmail/aws tools. Pass a frozenset to opt in to narrower
    gating (e.g. ``frozenset({"infrastructure"})`` for SRE-only tools).
    Roles whose ``tools`` list IS empty (e.g. ``general``) always see
    every tool regardless of this field."""

    def to_openai_schema(self) -> dict[str, Any]:
        """OpenAI function-tool schema shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# Module-level cache; populated by ``get_registry()`` on first call.
_REGISTRY: dict[str, ToolSpec] | None = None


def _iter_tool_modules() -> Iterable[str]:
    """Yield fully-qualified module names under ``app.tools``."""
    # Lazy import to avoid a circular import at package init time.
    from . import tools as tools_pkg
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        # Skip dunder + private modules; helpers like ``google_creds`` don't
        # export a TOOL_SPEC, so the absence check below catches them anyway.
        if info.name.startswith("_"):
            continue
        yield f"app.tools.{info.name}"


def _collect_from_module(module_name: str) -> list[ToolSpec]:
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        logger.warning("tool_registry: failed to import %s: %s", module_name, e)
        return []

    out: list[ToolSpec] = []
    spec = getattr(mod, "TOOL_SPEC", None)
    if isinstance(spec, ToolSpec):
        out.append(spec)
    specs = getattr(mod, "TOOL_SPECS", None)
    if isinstance(specs, (list, tuple)):
        for s in specs:
            if isinstance(s, ToolSpec):
                out.append(s)
            else:
                logger.warning("tool_registry: %s.TOOL_SPECS contains non-ToolSpec: %r", module_name, s)
    return out


def discover_tools() -> list[ToolSpec]:
    """Walk ``app/tools`` and collect every exported ``ToolSpec``.

    Bypasses the cache — call ``get_registry()`` for the cached dict view.
    """
    out: list[ToolSpec] = []
    seen: set[str] = set()
    for module_name in _iter_tool_modules():
        for spec in _collect_from_module(module_name):
            if spec.name in seen:
                logger.warning("tool_registry: duplicate tool name %s in %s", spec.name, module_name)
                continue
            seen.add(spec.name)
            out.append(spec)
    return out


def get_registry() -> dict[str, ToolSpec]:
    """Return the cached name → ToolSpec dict."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = {s.name: s for s in discover_tools()}
        logger.info("tool_registry: discovered %d tools: %s",
                    len(_REGISTRY), sorted(_REGISTRY.keys()))
    return _REGISTRY


def reset_registry_for_tests() -> None:
    """Drop the cache so tests can re-discover after monkeypatching."""
    global _REGISTRY
    _REGISTRY = None


def get_tool_names() -> set[str]:
    return set(get_registry().keys())


def get_spec(name: str) -> ToolSpec | None:
    return get_registry().get(name)


def get_default_roles_for(name: str) -> frozenset[str] | None:
    """Return the ``default_roles`` declared by the spec, or ``None`` for the
    uniform default."""
    spec = get_spec(name)
    return spec.default_roles if spec else None


def dispatch(func_name: str, func_args: dict, context: dict) -> str | None:
    """Run a tool from the registry. Returns ``None`` if no handler is set —
    caller falls through to a legacy inline branch."""
    spec = get_spec(func_name)
    if spec is None or spec.handler is None:
        return None
    try:
        return spec.handler(func_args or {}, context or {})
    except Exception as e:  # noqa: BLE001 — match the existing dispatch contract
        logger.exception("tool_registry: handler for %s raised", func_name)
        # Match the existing tool-call convention: errors surface to the model
        # as a JSON-shaped tool result rather than crashing the request.
        import json as _json
        return _json.dumps({"status": "error", "reason": str(e), "tool": func_name})


# ── role validation ───────────────────────────────────────────────────────

def validate_role_tool_names(role_tools: dict[str, list[str]]) -> list[str]:
    """Return the list of (role, tool) names that don't exist in the registry.

    Empty list means every role's ``tools`` list references known tools.
    The caller decides whether an unknown name is fatal at startup
    (recommended — see ``roles.py``).
    """
    known = get_tool_names()
    errors: list[str] = []
    for role_key, names in role_tools.items():
        for n in names:
            if n not in known:
                errors.append(f"role {role_key!r} lists unknown tool {n!r}")
    return errors
