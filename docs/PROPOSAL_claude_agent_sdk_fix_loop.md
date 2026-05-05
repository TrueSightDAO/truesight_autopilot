# Proposal: Use Claude Agent SDK for the fix-generation loop

**Status:** draft for implementation
**Audience:** the implementer (Kimi or any other agent)
**Author:** Claude (Opus 4.7), drafted 2026-05-03 in conversation with Gary
**Repo:** `truesight_autopilot/`

---

## 0. Why this doc exists

The implementer that's picking this up has reportedly disputed that the
Claude Agent SDK is a real package. It is. Verify before you implement:

- PyPI: https://pypi.org/project/claude-agent-sdk/
- GitHub: https://github.com/anthropics/claude-agent-sdk-python
- Docs: https://code.claude.com/docs/en/agent-sdk/overview
- Install: `pip install claude-agent-sdk` (Python ≥ 3.10)

The package was previously named `claude-code-sdk` and was renamed to
`claude-agent-sdk` to reflect that it's a general-purpose agent SDK,
not just a CLI wrapper. If `pip install claude-agent-sdk` succeeds and
`from claude_agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions`
imports cleanly, the package is real. If you can't reach PyPI from
your sandbox, that's an environment problem — not evidence the package
doesn't exist.

---

## 1. The problem

`truesight_autopilot/` today watches Gmail + AWS for failures. The
watch layer works. The fix layer does not:

- `app/email_poller.py` has a `# TODO: implement actual code edit + PR
  open` block followed by `pass`.
- `app/llm_client.py` (post the `governor_chatbot_service` merge) is
  DeepSeek-only with tool_calls support — usable as a low-level tool
  loop, but you'd be reimplementing Read/Edit/Write/Bash/Grep/Glob
  from scratch around it.
- `app/github_client.py` admits the workflow-log fetcher uses job
  names as a proxy because raw log download is unimplemented.

The hard part of the autopilot is not polling — it's the agentic loop:
*read a failure email → grep the repo → form a hypothesis → edit a
file → run `py_compile`/tests → iterate until green → open a PR.*
Building that on top of `httpx.post(...)` calls plus DeepSeek
tool_calls is a quarter of work that's already shipped in the Claude
Agent SDK.

The Claude Agent SDK collapses that to "spawn a session with a tight
system prompt and the right allowed tools." It exposes the same
Read/Edit/Write/Bash/Grep/Glob primitives Claude Code uses, plus
permission modes, hooks, and MCP — battle-tested.

---

## 2. Cost guardrail (read first)

The original README pitched DeepSeek-V3 because it's *~30× cheaper
than Claude*. Don't undo that with a naive swap. The discipline:

| Tier | What | Model | Cost |
|------|------|-------|-----:|
| 1 | Subject-line + sender regex match (rule-based) | none | $0 |
| 2 | "Is this email actionable? What kind?" classifier | DeepSeek-V3 (existing) | ~$0.0001 / call |
| 3 | "Read the repo, write the fix, open the PR" agent loop | **Claude (SDK)** | ~$0.10–$1 / fix attempt |

Tier 1 + Tier 2 already filter aggressively — `MAX_PR_PER_DAY=5` is a
hard cap. So Tier 3 cost is bounded at roughly $5/day worst-case.
That's the budget. The SDK only runs at Tier 3.

Pick `claude-haiku-4-5` as the default model for the agent loop. It's
the cheapest agentic-quality tier, and bug fixes are usually
small-scope. Promote to `claude-sonnet-4-6` for repos flagged as
high-stakes (controlled by an env var like `AUTOPILOT_MODEL_TIER`).

> **Note for the implementer:** The May 3 merge that removed the
> Anthropic fallback from `llm_client.py` is not a barrier — this
> proposal does not put Claude back into `llm_client.py`. The SDK is
> a separate dependency invoked only from `app/fix_agent.py` at
> Tier 3. DeepSeek remains the sole client for Tier 2 chat / context
> ingestion / classification.

---

## 3. Architecture changes

### Files to add

```
app/fix_agent.py           # NEW — Claude Agent SDK wrapper for Tier 3
app/sandbox.py             # NEW — git-worktree-per-fix isolation
app/safety_hooks.py        # NEW — PreToolUse hooks for hard limits
docs/PROPOSAL_claude_agent_sdk_fix_loop.md  # this file
```

### Files to modify

```
app/email_poller.py        # replace the TODO/pass with fix_agent.attempt_fix(...)
app/config.py              # add ANTHROPIC_API_KEY, AUTOPILOT_MODEL_TIER, FIX_AGENT_MAX_TURNS
requirements.txt           # add claude-agent-sdk>=0.1.0
```

### Files to leave alone

```
app/llm_client.py          # KEEP — DeepSeek for Tier 2 + governor chat
app/aws_monitor.py         # not in scope
app/edgar_logger.py        # called from fix_agent on success, no internal changes
app/auth.py / context.py / governor_registry.py / tools/   # all stay as-is
```

---

## 4. `app/fix_agent.py` — concrete skeleton

```python
"""Tier 3 fix loop: spawn a Claude Agent SDK session against a
sandboxed repo worktree, let it diagnose + edit + open a PR, return
result."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    HookMatcher,
)

from .config import settings
from .sandbox import RepoWorktree
from .safety_hooks import deny_dangerous_bash, deny_writes_outside_cwd

logger = logging.getLogger("autopilot.fix_agent")


@dataclass
class FixAttemptResult:
    success: bool
    pr_url: str | None
    summary: str
    turns_used: int
    cost_usd: float | None  # None if not reported by the SDK


SYSTEM_PROMPT = """\
You are an autonomous SRE working on TrueSight DAO infrastructure. You
have been invoked because a CI workflow or a Google Apps Script raised
an error. Your job:

1. Read the failure context provided in the user message.
2. Use Read, Grep, Glob, Bash to understand the codebase.
3. Identify the root cause.
4. Make the smallest possible fix using Edit / Write.
5. Verify the fix locally with `python -m py_compile <file>` or any
   project test command you can find in CI config / Makefile.
6. Commit the change to a new branch, push, open a PR with a clear
   description that names the failure and links to it.
7. Stop.

Hard rules:
- Never push to main / master directly. Always go via a feature branch.
- Never run `git push --force` or `git reset --hard`.
- Never run `--no-verify` or skip hooks.
- Stay inside the working directory provided. No filesystem writes
  outside of it.
- If you can't form a clear hypothesis after exploring, stop and
  report failure rather than guess.
- Keep diffs small. One logical fix per PR.
"""


async def attempt_fix(
    *,
    repo: str,             # e.g. "TrueSightDAO/dao_client"
    failure_summary: str,  # what email_poller distilled
    failure_context: str,  # log tail, error trace, etc.
) -> FixAttemptResult:
    """Run one fix attempt against a clean worktree of `repo`."""
    async with RepoWorktree(repo) as wt:
        options = ClaudeAgentOptions(
            cwd=str(wt.path),
            model=_pick_model(),
            system_prompt=SYSTEM_PROMPT,
            allowed_tools=[
                "Read", "Grep", "Glob",        # exploration
                "Edit", "Write",                # code mods
                "Bash",                         # py_compile, tests, git, gh
            ],
            permission_mode="acceptEdits",     # auto-approve file edits
            max_turns=settings.fix_agent_max_turns,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[deny_dangerous_bash],
                    ),
                    HookMatcher(
                        matcher="(Edit|Write)",
                        hooks=[deny_writes_outside_cwd(str(wt.path))],
                    ),
                ],
            },
        )

        prompt = _build_prompt(repo, failure_summary, failure_context)

        last_message = None
        turns = 0
        async for msg in query(prompt=prompt, options=options):
            last_message = msg
            turns += 1

        return _build_result(last_message, turns)


def _pick_model() -> str:
    tier = (settings.autopilot_model_tier or "haiku").lower()
    return {
        "haiku":  "claude-haiku-4-5",
        "sonnet": "claude-sonnet-4-6",
        "opus":   "claude-opus-4-7",
    }.get(tier, "claude-haiku-4-5")


def _build_prompt(repo: str, summary: str, context: str) -> str:
    return f"""\
Repo: {repo}
Working directory: . (already cloned)

Failure summary
---------------
{summary}

Failure context (logs / trace / email body)
-------------------------------------------
{context}

Diagnose, fix, push a branch, open a PR. When done, print the PR URL
on its own line prefixed with `PR: `.
"""


def _build_result(last_msg, turns) -> FixAttemptResult:
    # ResultMessage fields per the SDK: .result (final assistant text),
    # .total_cost_usd (if available).
    text = getattr(last_msg, "result", "") or ""
    pr_url = None
    for line in text.splitlines():
        if line.strip().startswith("PR: "):
            pr_url = line.strip()[len("PR: "):].strip()
            break
    return FixAttemptResult(
        success=pr_url is not None,
        pr_url=pr_url,
        summary=text[:500],
        turns_used=turns,
        cost_usd=getattr(last_msg, "total_cost_usd", None),
    )
```

---

## 5. `app/sandbox.py` — worktree isolation

Each fix attempt runs in its own throwaway clone so concurrent
attempts can't clobber each other and a misbehaving agent can't
corrupt a long-lived clone.

```python
"""Throwaway clone isolation for fix attempts."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


class RepoWorktree:
    """Async context manager: clones <repo> at HEAD into a temp dir,
    cleans up on exit. The agent commits + pushes + opens the PR via
    `gh` from inside the worktree; we only own the local copy."""

    def __init__(self, repo: str):
        self.repo = repo  # "owner/name"
        self.path: Path | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None

    async def __aenter__(self) -> "RepoWorktree":
        self._tmp = tempfile.TemporaryDirectory(prefix="autopilot-")
        self.path = Path(self._tmp.name) / "repo"
        url = f"https://github.com/{self.repo}.git"
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "50", url, str(self.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed for {self.repo}")
        return self

    async def __aexit__(self, *exc):
        if self._tmp is not None:
            self._tmp.cleanup()
```

Notes for the implementer:
- `--depth 50` is a hint, not a rule. Bump if a specific repo needs it.
- The agent runs `gh pr create ...` from inside the worktree using
  the bot's PAT. The bot user must have `Contents: Read+Write` and
  `Pull requests: Read+Write` on every target repo.

---

## 6. `app/safety_hooks.py` — hard limits the agent cannot override

```python
"""PreToolUse hooks. Return permission_decision='deny' to block."""
from __future__ import annotations

import os
import re

DANGEROUS_BASH = re.compile(
    r"\b("
    r"git\s+push\s+(--force|-f)\b"
    r"|git\s+reset\s+--hard"
    r"|--no-verify"
    r"|rm\s+-rf\s+/"
    r"|sudo\b"
    r"|curl\s.*\|\s*(sh|bash)"   # piping the internet to a shell
    r")"
)


async def deny_dangerous_bash(input_data, tool_use_id, context):
    cmd = (input_data.get("tool_input", {}) or {}).get("command", "")
    if DANGEROUS_BASH.search(cmd):
        return {
            "hookSpecificOutput": {
                "hookEventName": input_data["hook_event_name"],
                "permissionDecision": "deny",
                "permissionDecisionReason":
                    f"safety_hooks: refused dangerous bash: {cmd[:80]}",
            }
        }
    return {}


def deny_writes_outside_cwd(allowed_root: str):
    """Factory: returns a hook that denies Edit/Write outside allowed_root."""
    allowed_real = os.path.realpath(allowed_root)

    async def _hook(input_data, tool_use_id, context):
        path = (input_data.get("tool_input", {}) or {}).get("file_path", "")
        if not path:
            return {}
        target = os.path.realpath(path)
        if not target.startswith(allowed_real + os.sep) and target != allowed_real:
            return {
                "hookSpecificOutput": {
                    "hookEventName": input_data["hook_event_name"],
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        f"safety_hooks: write outside worktree blocked: {target}",
                }
            }
        return {}

    return _hook
```

These are belt-and-suspenders. The `cwd` setting in
`ClaudeAgentOptions` already scopes the agent. The hooks are the
safety net for prompt-injected attempts to escape.

---

## 7. `app/email_poller.py` change

Replace the existing `# TODO: implement actual code edit + PR open` /
`pass` block. Pseudo-diff:

```python
# OLD
            workflow_name="unknown",  # TODO: extract from email
            run_url=run_url,
            failure_message=failure_summary,
        )
        # TODO: implement actual code edit + PR open
        pass

# NEW
            workflow_name=workflow_name,
            run_url=run_url,
            failure_message=failure_summary,
        )
        from .fix_agent import attempt_fix
        result = await attempt_fix(
            repo=repo,
            failure_summary=failure_summary,
            failure_context=workflow_log_tail,
        )
        if result.success:
            edgar.log_contribution(
                minutes=int(result.turns_used * 1.5),  # rough proxy
                description=(
                    f"[autopilot] Fixed {workflow_name} in {repo}. "
                    f"{result.summary[:200]}"
                ),
                pr_url=result.pr_url,
            )
        else:
            logger.warning(
                "fix_agent gave up after %d turns: %s",
                result.turns_used, result.summary[:200],
            )
```

`workflow_log_tail` today is weak — `github_client.py:fetch_workflow_log`
returns job names instead of log content. Either:

(a) fix `fetch_workflow_log` to actually download the log tarball and
    return the last N lines (recommended), or
(b) hand the agent the workflow run URL and let it call
    `gh run view --log-failed <id>` itself via Bash.

Option (b) is fine for the first cut — the agent can drive `gh` itself.

---

## 8. `app/config.py` additions

```python
    # Claude Agent SDK (Tier 3 fix loop)
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    autopilot_model_tier: str = os.getenv("AUTOPILOT_MODEL_TIER", "haiku")
    fix_agent_max_turns: int = int(os.getenv("FIX_AGENT_MAX_TURNS", "20"))
```

`ANTHROPIC_API_KEY` must be set on the EC2. If you'd rather route via
Bedrock, set `CLAUDE_CODE_USE_BEDROCK=1` plus AWS creds — the SDK
supports it transparently. (Same for Vertex AI / Foundry.)

---

## 9. `requirements.txt` addition

```
claude-agent-sdk>=0.1.0
```

That's it. The SDK bundles the `claude` CLI binary it talks to over
stdio internally; you do **not** need to install Claude Code
separately on the host.

---

## 10. SDK API reference for the implementer

Authoritative facts, current as of 2026-05-03. Verify each at the
linked source:

**Package & install** — `pip install claude-agent-sdk`
([PyPI](https://pypi.org/project/claude-agent-sdk/),
[GitHub](https://github.com/anthropics/claude-agent-sdk-python)).
Python ≥ 3.10.

**Two entry points:**

```python
# One-shot (fresh session each call) — what fix_agent.py uses
from claude_agent_sdk import query, ClaudeAgentOptions
async for message in query(prompt="...", options=ClaudeAgentOptions(...)):
    ...

# Multi-turn session — not needed for fix_agent, but available
from claude_agent_sdk import ClaudeSDKClient
async with ClaudeSDKClient(options=options) as client:
    await client.query("...")
    async for msg in client.receive_response():
        ...
```

**`ClaudeAgentOptions` fields used here:**
- `cwd: str` — working directory for the session
- `model: str` — `"claude-haiku-4-5"`, `"claude-sonnet-4-6"`, or `"claude-opus-4-7"`
- `system_prompt: str` — appended/replaces default
- `allowed_tools: list[str]` — built-ins: Read, Write, Edit, Bash,
  Glob, Grep, WebSearch, WebFetch, Agent, AskUserQuestion
- `disallowed_tools: list[str]` — always blocked
- `permission_mode: str` — one of:
  - `"default"` — unmatched tools fall through to a `canUseTool` callback
  - `"dontAsk"` — deny instead of prompting
  - `"acceptEdits"` — auto-approve file ops (what we use)
  - `"bypassPermissions"` — allow all (do not use)
  - `"plan"` — plan-only, no execution
- `max_turns: int` — iteration cap
- `hooks: dict[str, list[HookMatcher]]` — see below
- `mcp_servers: dict` — for custom tools (see §11)

**`HookMatcher` and PreToolUse hook signature:**

```python
from claude_agent_sdk import HookMatcher

async def my_hook(input_data: dict, tool_use_id: str | None, context) -> dict:
    # input_data keys: tool_name, tool_input, session_id,
    #                  hook_event_name, cwd
    return {
        "hookSpecificOutput": {
            "hookEventName": input_data["hook_event_name"],
            "permissionDecision": "deny",  # or "allow", "ask"
            "permissionDecisionReason": "...",
        }
    }
```

Register:

```python
options = ClaudeAgentOptions(
    hooks={
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[my_hook]),
        ],
    },
)
```

Other hook events: `PostToolUse`, `UserPromptSubmit`, `Stop`,
`SubagentStop`, `PreCompact`.

Docs:
- Overview: https://code.claude.com/docs/en/agent-sdk/overview
- Permissions: https://code.claude.com/docs/en/agent-sdk/permissions
- Hooks: https://code.claude.com/docs/en/agent-sdk/hooks
- Python reference: https://code.claude.com/docs/en/agent-sdk/python
- MCP: https://code.claude.com/docs/en/agent-sdk/mcp

**Transport / auth:** the SDK bundles its own `claude` CLI binary and
talks to it over stdio internally — no separate install needed. Auth
is via `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_USE_BEDROCK=1` /
`CLAUDE_CODE_USE_VERTEX=1` / `CLAUDE_CODE_USE_FOUNDRY=1` for cloud
routing). All
[here](https://code.claude.com/docs/en/agent-sdk/overview).

---

## 11. Future: MCP server for Edgar (optional, not v1)

Once the fix agent is working, the cleanest way to let it log its own
contributions is an in-process MCP server:

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool(
    "submit_contribution",
    "Log autopilot work to Edgar",
    {"minutes": int, "description": str, "pr_url": str},
)
async def edgar_submit(args):
    # Wrap dao_client.modules.report_contribution
    ...

edgar_server = create_sdk_mcp_server(
    name="edgar", version="1.0.0", tools=[edgar_submit],
)

options = ClaudeAgentOptions(
    mcp_servers={"edgar": edgar_server},
    allowed_tools=[..., "mcp__edgar__submit_contribution"],
)
```

MCP tool naming: `mcp__<server-name>__<tool-name>`.

This lets the agent end its session with a single tool call instead of
the wrapper code in `email_poller.py`. Cleaner, but skip for v1.

---

## 12. Implementation order

1. `pip install claude-agent-sdk` and verify
   `import claude_agent_sdk` succeeds. **Stop here** if your
   environment can't install — that's the blocker, not a missing
   package.
2. Add `app/sandbox.py` with a unit test that clones a small public
   repo and cleans up.
3. Add `app/safety_hooks.py` and unit-test the regex against:
   - `git push origin feat/foo` → allow
   - `git push --force origin main` → deny
   - `rm -rf /` → deny
   - `python -m py_compile foo.py` → allow
4. Add `app/fix_agent.py`. Write an integration test that runs against
   a deliberately-broken sandbox repo containing one obvious typo
   (e.g. `prnt("hi")`). Expect it to open a PR fixing the typo.
5. Wire `email_poller.py` to call `fix_agent.attempt_fix`.
6. `DRY_RUN=true` deploy to EC2 first; verify the loop logs intended
   actions. Flip live.

---

## 13. Auth & credentials checklist

Per the 2026-05-03 credential audit
(`agentic_ai_context/notes/claude_autopilot_credential_audit_2026-05-03.md`),
these gate go-live:

- [ ] `ANTHROPIC_API_KEY` — provisioned for the autopilot, billed to
      the org, separate from any personal key.
- [ ] Dedicated `truesight-autopilot` GitHub user with a PAT that has
      `Contents: Read+Write` and `Pull requests: Read+Write` on every
      target repo. The current PAT only has write access to `.github`.
- [ ] AWS IAM instance role on the EC2 host (current keys are dead).
- [ ] Dedicated Edgar keypair for `autopilot@agroverse.shop`. Current
      keys in `dao_client/.env` are Gary's personal identity — using
      them would attribute every autopilot fix to him.

None of these are SDK concerns, but they all gate flipping `DRY_RUN`
off in production.

---

## 14. What this proposal is NOT

- **Not a rewrite of `llm_client.py`.** DeepSeek stays as-is for
  governor chat + Tier 2 classification.
- **Not vendor lock-in below the agent loop.** Tier 1 + Tier 2 still
  use deterministic rules + DeepSeek.
- **Not auto-merge.** Hard rule. The agent opens PRs; humans merge.
- **Not "use Claude for everything."** Claude is the surgeon at
  Tier 3 only. Tiers 1 + 2 do the triage cheaply.
