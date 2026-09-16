# PR #422 — Supersession Analysis

**Author:** Sophia Truesight (admin+sophia@truesight.me)
**Date:** 2026-09-16
**PR under review:** #422 — `fix(dao): auto-compute TDG from rubric in create_dao_submission, never hard-default 0`
**Verdict: SUPERSEDED — close, do not merge.**

---

## 1. Summary

Gary's request was to push the #422 resolution work to Git for review. The autopilot box's `gh`
(v2.4.0, PAT lacks `pull_request:write`) cannot close a PR, and `merge_pr` refuses an unmergeable
one, so the decision needs a human. This document is the evidence for a one-click close.

I rebased the full 13-commit branch onto current `main` in a scratch worktree
(`/tmp/pr422wt`, removed afterwards — the deploy tree was never touched).

## 2. What #422 intended, and where it already exists

| #422's intended change | Status on `main` today |
|---|---|
| Auto-compute TDG from the rubric SSOT; never hard-default `"0"` | ✅ `app/main.py:_resolve_tdg_issued()` |
| Governor non-zero override wins; absent / `""` / `"0"` / unparseable → rubric | ✅ same semantics |
| CLI: `--type` / `--minutes` / `--usd`; drop unsupported `--amount` / `--tdg-issued` | ✅ `app/tools/dao_submission.py` |
| Tests | ✅ main's are stronger: `tests/test_resolve_tdg_issued.py` (9 asserts, incl. `$1,234.5`, unknown-type fallback) |

Main gained these via **#438**; the rationale comment cites the Cristo Rei / Sitio Torres FSVP
zeroing regression (2026-09) and Gary's standing rule (2026-09-10, thread 24442,
`DAO_CLIENT_AI_AGENT_CONTRIBUTIONS.md` rule 5 — 100 TDG per Time hour; 1:1 for USD).

## 3. Conflict breakdown (8 conflicts, 6 files)

Seven of eight were **already-upstream** (git auto-dropped them as "patch contents already upstream")
or trivial:

- `.gitignore` — #460's block already on main.
- #412 files, #417 `app/config.py`, #419 files — main carries stricter/later versions.
- #421 `app/main.py` `_REDACTION_PATTERNS` — main consolidated redaction into `app/redaction.py`
  (single source of truth); `main.py` now only has a thin `_redact_secrets()` wrapper.

The **single semantic conflict** was `_run_tool_sync` in `app/main.py`. On inspection, main had
already implemented #422's fix there under a *better* name (`_resolve_tdg_issued`), with a docstring
and dedicated tests.

## 4. The decider — post-rebase residual

After resolving everything to main's side, the branch's **only** remaining contribution was
`tests/test_dao_submission_rubric.py` — which imports and calls `compute_tdg_from_rubric`, a symbol
that **does not exist on `main`**:

```
AttributeError: module 'app.tools.dao_submission' has no attribute 'compute_tdg_from_rubric'
6 failed, 2 passed
```

**Merging #422 would break CI.** It has no unique value to preserve, so it should be closed as
superseded rather than resurrected as a duplicate.

## 5. Secondary finding — `python3 -m pytest` is a false-red trap on this box

The house rule "run the suite locally before pushing" is written as `python3 -m pytest`. On the
autopilot box that binds the **stale system-wide** `truesight_dao_client` (0.2.0, no `rubric.py`):

| interpreter | `truesight_dao_client.rubric` | `tests/test_resolve_tdg_issued.py` |
|---|---|---|
| `python3` (system) | ❌ ImportError | **5 failed** |
| `./.venv/bin/python` | ✅ present | **8 passed** |

The service runs `./.venv/bin/python`; CI installs fresh from `requirements.txt`. So this is a
**local-tooling trap only** — it makes a green `main` look broken and could mask a real regression
in a future run.

## 6. Test status (honest)

- `./.venv/bin/python -m pytest`: **1263 passed**, 2 failed
  (`test_config_own_data_repos`, `test_email_inbox_watch`) — pre-existing, order/isolation-dependent
  (both pass in isolation: 26 passed), unrelated to a docs-only change.
- `python3 -m pytest`: 5 red — the §5 trap.
- This change adds one markdown file; no code paths touched.

## 7. Recommendations

1. **Close #422** as superseded (human, or a token with PR-close scope).
2. Harden the local-test rule to prefer `./.venv/bin/python -m pytest`, or add a conftest guard that
   fails loudly when `truesight_dao_client.rubric` is absent.

— Sophia Truesight
