# Authoring autopilot tools

The autopilot agent's tool surface lives in this directory. Each tool is a
Python function plus a small declarative `TOOL_SPEC` (or `TOOL_SPECS`) entry
that the [capability manifest](../tool_registry.py) auto-discovers.

**Before this layer existed** (pre-2026-05-28), adding a new tool meant
editing **four** places: the function, the schema in `llm_client.py`, the
dispatch branch in `main.py`, and the role list in `roles.py`. That's where
the `merge_pr` role-gating bug hid for weeks. The manifest collapses the
four-place edit to **one place per tool**.

## The 30-second version

```python
# app/tools/echo_tool.py
"""A one-line description of what this module does."""
from __future__ import annotations
import json

from ..tool_registry import ToolSpec


def echo_tool(message: str) -> str:
    """Implementation function — pure Python, no LLM concerns."""
    return json.dumps({"status": "ok", "echo": message})


TOOL_SPEC = ToolSpec(
    name="echo_tool",
    description="Return the message you were given. Useful for testing the agent.",
    parameters={
        "type": "object",
        "properties": {"message": {"type": "string", "description": "What to echo."}},
        "required": ["message"],
    },
    handler=lambda args, ctx: echo_tool(args.get("message", "")),
    default_roles=None,  # see "Role gating" below
)
```

That's the entire change. Drop the file into `app/tools/`. The next time the
service boots:

- `app/llm_client.get_tool_schemas()` auto-discovers and includes the schema.
- `app/main._run_tool()` dispatches `echo_tool` calls into the handler.
- `app/roles.py` validation accepts `"echo_tool"` in any role's `tools` list.

No edits to `llm_client.py` / `main.py` / `roles.py` required.

## TOOL_SPEC fields

| Field | Required | Notes |
|---|---|---|
| `name`        | yes | Snake-case, unique across the manifest. Becomes the OpenAI function name. |
| `description` | yes | One paragraph. **The model reads this verbatim** to decide whether to call your tool. Be specific about what it does, what it returns, and when to prefer a sibling tool. |
| `parameters`  | yes | JSON Schema dict for the function's arguments. Keep types tight; use `enum` where applicable; mark `required` honestly. |
| `handler`     | no  | `(args: dict, context: dict) -> str`. If `None`, dispatch falls through to a legacy inline branch in `main.py` — used by orchestration tools (see below). For every new tool, set this. |
| `default_roles` | no | `None` (default) = every non-empty role gets the tool. Pass a `frozenset` of role keys to gate tighter, e.g. `frozenset({"infrastructure"})` for an SRE-only tool. The `general` role always sees every tool regardless. |

## The handler contract

`handler(args, context) -> str`

- `args` — the kwargs the model passed; pre-validated against `parameters` by
  the LLM provider, but **always** call `.get()` with a default to be defensive.
- `context` — session state: `{"history": list[dict], "session_id": str | None,
  "governor_name": str | None}`. Most simple tools ignore this.
- Return value — a string. By convention, a JSON-encoded dict with at least
  `{"status": "ok" | "error", ...}`. The Tavily web tools, Gmail tools, AWS
  tool, etc. are good shape references.

**Never** raise from a handler in steady state. Catch and return `{status:
"error", reason: <str>}`. The registry has a top-level try/except that turns
exceptions into error JSON anyway, but that's a safety net, not a contract.

## Multi-tool modules

Modules that bundle several related operations (e.g. `gmail_tools.py` exposes
six) export `TOOL_SPECS` as a list:

```python
TOOL_SPECS = [
    ToolSpec(name="gmail_search", ...),
    ToolSpec(name="gmail_read_message", ...),
    ...
]
```

`TOOL_SPEC` and `TOOL_SPECS` may both be present — discovery handles either.

## Role gating

Every role in `app/roles.py` has an explicit `tools` list (or empty = all).
The startup validator (`_validate_role_tool_names` in `roles.py`) raises if
any role lists a tool name not in the manifest — catching the typo class of
bug that hid `merge_pr` from the `infrastructure` role.

To make your tool accessible to a specific role:
1. Add the tool's `name` to that role's `tools` list in `app/roles.py`, OR
2. Leave `default_roles=None` on the spec and the **uniform default** (every
   non-empty role) will surface it. Then a future refactor can wire that
   default through, but right now `roles.py` is still the authoritative
   per-role list — `default_roles` is informational pending that next step.

The `general` role (empty `tools` list) always sees every tool. So even with
no explicit role gating, an operator can switch to `general` to use a new
tool.

## When `handler=None` is the right choice

Four existing tools dispatch inline in `app/main._run_tool`:

- `submit_contribution` — pre-flight duplicate guards, history-based approval
  gate, governor-name injection, `_add_pending` persistence to GitHub.
- `create_dao_submission` — pulls `governor_name` for the contributor field.
- `open_fix_pr` — orchestrates the `FixAgent` loop + `_add_pending`.

Their `TOOL_SPEC` entries (in `dao_submission.py` and `orchestration_specs.py`)
have `handler=None` so the registry skips them and the existing inline
branches handle dispatch. This keeps the manifest authoritative for **schema
+ role gating** without forcing a risky refactor of the session-state code.

**Prefer a real handler.** Only use `handler=None` when the tool genuinely
needs the session-level state currently held in `main.py`'s closures.

## How dispatch actually works

```
LLM tool-call → _run_tool(func_name, func_args, history, session_id, governor_name)
  │
  ├── tool_registry.dispatch(func_name, args, ctx)
  │    └── if spec.handler is set → call it → return its string  ✅ done
  │    └── if spec.handler is None or tool unknown → return None
  │
  └── (only reached when registry returned None)
       Legacy inline branches: `if func_name == "submit_contribution": ...`
```

So the registry **shadows** the legacy branches whenever a handler exists.
For new tools, that's the only path. For the four orchestration tools, the
inline branches remain authoritative until they're migrated.

## Schema description tips for the LLM

The model decides whether to call your tool based purely on `description` +
`parameters.properties[*].description`. A few things worth being explicit
about:

- **Return shape.** "Returns JSON with `status`, `message`, …" lets the
  model reason about chaining.
- **Side effects.** Send/mutating tools should say so loudly — see
  `gmail_send` ("Use sparingly — sending is irreversible") and `merge_pr`
  ("Only use when a governor explicitly tells you to merge").
- **Sibling guidance.** When you ship a tool that could be confused with
  another, mention the contrast — see `gmail_create_draft` ("Preferred over
  gmail_send when the user hasn't explicitly approved sending").
- **Pre-existing tools the agent should chain.** `generate_pdf` mentions
  "Pair with `upload_file_to_github(content_base64=…)`" so the agent learns
  the workflow.

## Testing

Drop a `tests/test_<your_tool>.py` next to the existing tool tests. Pattern:

```python
def test_credentials_missing_returns_error(monkeypatch):
    monkeypatch.setattr(my_tool, "load_credentials", lambda *a, **k: None)
    out = json.loads(my_tool.my_tool("..."))
    assert out["status"] == "error"
```

Mock external clients (Google APIs, AWS, GitHub, HTTP). Don't fake-test the
network. See `tests/test_google_sheets.py`, `tests/test_aws_tools.py`,
`tests/test_gmail_tools.py` for examples.

If your tool would only get *uniformly default* role access, `tests/test_tool_registry.py`
already covers role-gating regression. If you gate to a specific role, add an
assertion in `tests/test_roles_tool_gating.py` so it stays gated.

## Reference

- `app/tool_registry.py` — the manifest layer.
- `agentic_ai_context/plans/AUTOPILOT_CAPABILITY_MANIFEST_PLAN.md` — design context.
- Examples worth reading before authoring your first tool:
  - Simple wrapper: `app/tools/web_search.py`
  - Multi-op module: `app/tools/gmail_tools.py`
  - Auth-heavy: `app/tools/google_sheets.py`
  - Inline orchestration (handler=None): `app/tools/dao_submission.py`
