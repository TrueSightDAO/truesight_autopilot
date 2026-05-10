# Integration tests — autopilot `/chat`

End-to-end tests that exercise the streaming chat path against a **running**
autopilot instance. They're not pure unit tests (they hit a live LLM
provider, real `httpx` over real SSE), so they ship as a separate
directory rather than as part of the smoke CI.

## What's covered

| File | Validates |
|---|---|
| `test_chat_smoke.py` | One-tool-call round trip — proves chat path is alive end-to-end |
| `test_chat_interjection.py` | SSE heartbeat + mid-round queue interjection (PR #28) |
| `test_chat_cancel.py` | `DELETE /chat/active/{session_short}` abort (PR #29) |

## How to run

### 1. Start an autopilot instance

```bash
cd /Users/garyjob/Applications/truesight_autopilot
python scripts/launch_local_autopilot.py
# → daemon writes logs to /tmp/autopilot_8011.log, listens on 127.0.0.1:8011
```

Verify it's up: `curl http://127.0.0.1:8011/health`.

### 2. Run the tests

```bash
# All three
tests/integration/run_all.sh

# Or one at a time
.venv/bin/python tests/integration/test_chat_smoke.py
.venv/bin/python tests/integration/test_chat_interjection.py
.venv/bin/python tests/integration/test_chat_cancel.py
```

### 3. Clean up

```bash
pkill -f 'uvicorn.*8011'
```

## Configuration

| Env var | Default | What it does |
|---|---|---|
| `AUTOPILOT_URL` | `http://127.0.0.1:8011` | Override to test prod or a different local port |
| `DAO_CLIENT_ENV` | `/Users/garyjob/Applications/dao_client/.env` | Path to dotenv with `PUBLIC_KEY`/`PRIVATE_KEY` for signing |

## Why these are integration tests, not pytest

- Each one needs a **live** autopilot, an active LLM API key, and Gary's
  RSA key. Running them in CI without major harness work isn't worth it.
- The existing `tests/e2e_governor_chat.py` follows the same bespoke
  runner pattern (no pytest) for the same reasons.
- A `pytest`-friendly wrapper can be added later if/when there's a
  staging autopilot we want to gate releases on.

## Anti-patterns these tests have caught (so far)

- Connection-drop on long tool calls (`ChunkedEncodingError`) — caught
  by `test_chat_interjection.py` since it stages a stream that runs
  long enough to need the heartbeat.
- Cancellation flag not propagating through the heartbeat loop — would
  surface as `test_chat_cancel.py` timing out instead of receiving a
  `cancelled` event.
- Refactor regression in PR #32 (loop consolidation) — both tests were
  re-run post-refactor before merge.

## Adding a new test

1. Drop a `test_chat_<thing>.py` in this directory.
2. Import `_autopilot_client` for the `GovernorKey`, `stream_chat`,
   `queue_message`, `cancel_chat` helpers.
3. End your `main()` with `return 0` on pass, `1` on fail.
4. Add the file name to `run_all.sh`.

`do_not_publish=True` is the default in `stream_chat()` so test runs
don't pollute the public transcript repo. Override only if you're
explicitly testing the publish path.
