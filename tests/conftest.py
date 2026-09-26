"""Shared pytest configuration for the hermetic unit suite.

Two responsibilities:

1. Redirect writable base paths (session logs, vault) to a per-run temp
   directory BEFORE any ``app.*`` module is imported. The production
   defaults / committed ``.env`` point at ``/opt/truesight_autopilot``,
   which is a real box path that does not exist (and is not writable) in
   CI or on a developer laptop. Several modules ``mkdir`` these paths at
   import time, so the override has to happen at conftest load — before
   test collection imports the application code.

2. Run ``@pytest.mark.asyncio`` coroutine test functions. The project
   convention is plain sync tests that wrap async work in
   ``asyncio.run(...)``; a couple of suites instead decorate
   ``async def`` tests with ``@pytest.mark.asyncio``. We do not ship
   pytest-asyncio, so this hook executes those coroutines without adding
   a dependency or changing the tests.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

# ── 1. Hermetic writable paths (must run before app.* imports) ─────────────
_TMP_BASE = tempfile.mkdtemp(prefix="autopilot_test_")
os.environ["SESSION_LOG_DIR"] = os.path.join(_TMP_BASE, "sessions")
os.environ["VAULT_DIR"] = os.path.join(_TMP_BASE, "vault")


# ── 2. Register + run @pytest.mark.asyncio coroutine tests ─────────────────
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "asyncio: run the test coroutine in a fresh event loop via asyncio.run",
    )


def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    """Execute coroutine test functions marked with @pytest.mark.asyncio."""
    if pyfuncitem.get_closest_marker("asyncio") is None:
        return None
    test_func = pyfuncitem.obj
    if not asyncio.iscoroutinefunction(test_func):
        return None
    # Pass only the arguments the test actually declares (fixtures, etc.).
    funcargs = pyfuncitem.funcargs
    sig_args = {
        name: funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
        if name in funcargs
    }
    asyncio.run(test_func(**sig_args))
    return True
