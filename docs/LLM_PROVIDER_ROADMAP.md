# LLM provider abstraction + usage logging — internal roadmap

This document is the autopilot-internal mirror of the public roadmap that lives at [`TrueSightDAO/truesight_autopilot_transcript/ROADMAP.md`](https://github.com/TrueSightDAO/truesight_autopilot_transcript/blob/main/ROADMAP.md). It scopes the same plan to the implementation files in *this* repo.

> **For agents picking this up:** read the public roadmap first for the rationale and phasing principles, then come back here for the file-level "what changes where".

---

## Why we want this

We currently have:

- `app/llm_client.py` — DeepSeek-V3 client, OpenAI-shaped, with a DeepSeek-specific XML tool-call fallback shim.
- `app/grok_client.py` — Grok vision client (OpenAI-shaped, multimodal).
- `app/gemini_client.py` — Gemini vision fallback (Google SDK, NOT OpenAI-shaped).
- A new `BIGMODEL_CN_API` key (BigModel.cn / Zhipu) sitting in `.env` from a DAO contributor, with no client wired up yet.

Three text-completion-eligible providers (DeepSeek, BigModel, Kimi) and two vision providers (Grok, Gemini). The cost of adding a fourth or fifth without a unified abstraction grows linearly — and worse, every quirk (XML tool calls, GLM tool-arg shapes, Gemini's non-OpenAI SDK) leaks into call sites.

We also have **zero per-call token tracking** today beyond a single `logger.info` line at `main.py:725-727`. The DAO can't audit LLM spend by provider, model, or subsystem.

---

## Target architecture (after Phase 6)

```
truesight_autopilot/app/
├── llm/
│   ├── __init__.py
│   ├── base.py                 # LLMProvider ABC, LLMResponse, LLMUsage
│   ├── openai_compatible.py    # shared HTTP plumbing (DeepSeek, BigModel, Kimi)
│   ├── deepseek.py             # XML tool-call quirk shim lives here
│   ├── bigmodel.py             # GLM-4.5+ provider
│   ├── kimi.py                 # (placeholder — re-add if needed)
│   ├── grok.py                 # Grok vision
│   ├── gemini.py               # Gemini vision (Google SDK adapter)
│   ├── registry.py             # name → provider class
│   └── usage_log.py            # writes JSONL to transcript repo
├── (deleted) llm_client.py
├── (deleted) grok_client.py
├── (deleted) gemini_client.py
└── ...
```

Call sites change from `LLMClient()` → `get_provider()` (default) or `get_provider("bigmodel")` (explicit override).

---

## Phase 0 — Documentation only ✅

- This document.
- Public roadmap in `truesight_autopilot_transcript`.

No code changes. No production risk.

---

## Phase 1 — Add `app/llm/` package alongside existing clients

**Files added:**
- `app/llm/__init__.py`
- `app/llm/base.py` — `LLMProvider` ABC, `LLMResponse`, `LLMUsage` dataclasses.
- `app/llm/openai_compatible.py` — shared HTTP plumbing.
- `app/llm/deepseek.py` — `DeepSeekProvider`, ports the XML tool-call fallback from `llm_client.py:133–207`.
- `app/llm/registry.py` — `_PROVIDERS` dict + `get_provider()`.
- `tests/llm/test_deepseek.py` — re-runs the same fixtures the legacy client passes.

**Files unchanged:** `app/llm_client.py`, every existing call site (`fix_agent.py`, `email_poller.py`, `main.py`).

**Production risk:** none. The new package is dead code on disk until Phase 2.

**Acceptance:**
- `pytest` green.
- `python -c "from app.llm import get_provider; p = get_provider('deepseek'); print(p.name)"` prints `deepseek`.

---

## Phase 2 — `LLMClient` becomes a shim

**File modified:** `app/llm_client.py` only.

**Plan:**
- `LLMClient.__init__` instantiates `DeepSeekProvider` from the new package and stores it.
- `LLMClient.chat`, `LLMClient.complete`, `LLMClient.extract_text`, `LLMClient.extract_tool_calls`, `LLMClient.diagnose_github_failure` all delegate to provider methods.
- Constructor signature, return shapes, exception types (`LLMError`) unchanged.
- `get_tool_schemas()` stays in `llm_client.py` (it's not provider-specific).

**Soak test:**
- Deploy to EC2 via `scripts/deploy.sh`.
- Watch `journalctl -u truesight-autopilot.service -f` for 3 days.
- Validate: governor chat, autonomous fix loop opening at least one PR, email poller diagnosing at least one failure.

**Production risk:** low. Same wire calls, same DeepSeek base URL, same response shapes. Only failure mode is import-error or signature mismatch — both caught by smoke test.

**Rollback:** `git revert` the shim PR.

---

## Phase 3 — Wire usage logging (off by default)

**Files added:**
- `app/llm/usage_log.py` — `UsageLogger` with batched flush + `gh api` write path.
- `tests/llm/test_usage_log.py`.

**Files modified:**
- `app/llm/base.py` — provider methods call `self._usage_logger.record(...)` after each successful call.
- `app/config.py` — add `llm_usage_log_enabled: bool` (default `False`).

**Behaviour:**
- When `LLM_USAGE_LOG_ENABLED=1`:
  - Chat path (`caller=chat`) writes per-session `usage.jsonl` to disk in `${SESSION_LOG_DIR}/<sid>/`. The existing transcript-emitting code already syncs session dirs to the transcript repo; usage.jsonl rides along.
  - Workers (`caller in {fix_agent, email_poller, aws_monitor, qr_scan_grok, qr_scan_gemini}`) batch records in memory; flush every 60 s OR at process exit, append to `usage/<date>/workers.jsonl` in the transcript repo via `gh api PUT /contents/...`.
- When the flag is unset: provider records nothing. Behaviour is identical to Phase 2.

**Schema:** `truesight_autopilot_transcript/SCHEMA.md` §3 and §4 are the source of truth. `LLMUsage` dataclass fields must match 1:1.

**Production risk:** none until enabled. When enabled: extra disk writes (cheap) + periodic git pushes to the transcript repo (1 push per worker per minute at most — well within rate limits).

**Acceptance:**
- Run with flag on for 1 week.
- Spot-check: `usage/<date>/_daily_summary.json` totals reconcile with DeepSeek's billing dashboard within ±5%.
- No new error log lines.

**Rollback:** Unset env var. Existing data in transcript repo stays.

---

## Phase 4 — `BigModelProvider` as opt-in

**Files added:**
- `app/llm/bigmodel.py` — extends `OpenAICompatibleProvider`, base URL `https://open.bigmodel.cn/api/paas/v4`.
- `tests/llm/test_bigmodel.py` — fixture-based, no live API call.

**Files modified:**
- `app/llm/registry.py` — register `"bigmodel"` → `BigModelProvider`.
- `app/config.py` — add `bigmodel_api_key`, `bigmodel_base_url`, `bigmodel_model`, `llm_provider` (default `"deepseek"`).

**Smoke tests (manual, before flipping prod):**
1. Set `LLM_PROVIDER=bigmodel` locally.
2. Run governor chat for 5–10 messages including one with `tool_calls`.
3. Run `fix_agent` against a sandbox repo or branch.
4. Run `email_poller` against a synthetic GH Actions failure email.
5. Confirm tool-call argument shape matches expectations (GLM sometimes returns parsed objects vs JSON strings).
6. Sample a handful of `usage.jsonl` lines and update pricing in `truesight_autopilot_transcript/PROVIDERS.md`.

**Production risk:** only if `LLM_PROVIDER` is flipped on a host without `BIGMODEL_CN_API`, OR if BigModel returns shapes the wrapper doesn't normalize. Mitigated by the manual smoke tests.

**Acceptance:** Manual smoke pass + `PROVIDERS.md` updated with sampled rates.

**Rollback:** Set `LLM_PROVIDER=deepseek`. BigModel becomes a no-op import.

---

## Phase 5 — Unify Grok / Gemini under the provider ABC (optional)

**Files moved:**
- `app/grok_client.py` → `app/llm/grok.py` (extending the ABC).
- `app/gemini_client.py` → `app/llm/gemini.py` (Google SDK adapter, not OpenAI-compatible).

**Files modified:**
- Any call site importing from the legacy paths (`app/tools/qr_scanner.py`, etc.).

**Caveat:** The QR scanner pipeline is actively in development as of 2026-05-08 (uncommitted vision-fallback work). **Do not start this phase until that work has stabilized.**

**Production risk:** medium — touching a code path that's still being shipped daily.

**Acceptance:** QR scanner tests pass; vision calls now appear in `_daily_summary.json` alongside text-completion calls.

---

## Phase 6 — Cleanup

- `grep -r "from .llm_client" app/ tests/` must return zero.
- `grep -r "from .grok_client\|from .gemini_client" app/ tests/` must return zero (if Phase 5 shipped).
- Delete the legacy files.
- Update inline comments referring to the legacy clients.

---

## Status as of 2026-05-08

| Phase | Status | Owner | Notes |
|---|---|---|---|
| 0 | 🟡 in this PR | autopilot maintainers | docs only |
| 1 | ⚪ not started | — | additive package |
| 2 | ⚪ not started | — | shim — gated on Phase 1 |
| 3 | ⚪ not started | — | usage logging — gated on Phase 2 |
| 4 | ⚪ not started | — | BigModel opt-in — gated on Phase 3 |
| 5 | ⚪ not started | — | optional; gated on QR pipeline stability |
| 6 | ⚪ not started | — | cleanup |

When a phase ships, update its row here AND on the public roadmap mirror.

---

## Why this is the right shape

1. **No phase deletes or renames anything that's running** until the next phase has soaked.
2. **Each phase is independently revertable** by `git revert` of a single PR.
3. **Default behaviour is unchanged** until `LLM_PROVIDER` is explicitly flipped (Phase 4+).
4. **Backwards-compat shims stay** until grep proves zero callers (Phase 6).

These principles exist because the autopilot is a live service: production EC2 systemd unit, background workers in continuous loops, governors interacting via `chat.html` at unpredictable times. The blast radius of a startup error is real — every refactor must be re-runnable in production without operator drama.
