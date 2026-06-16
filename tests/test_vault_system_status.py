"""Regression: vault status/deploy endpoints degrade when track_registry is absent.

`app.track_registry` is provided by a separate work-stream. When it is not yet
part of a build, `/vault/api/system-status` (and the deploy guard) must NOT 500 —
they fall back to "no tracks" so the vault page and deploys keep working.
"""

from __future__ import annotations

import asyncio
import sys


def test_system_status_degrades_without_track_registry(monkeypatch):
    from app import vault_routes

    # Setting the module to None forces `import` to raise ImportError,
    # deterministically exercising the fallback even after track_registry lands.
    monkeypatch.setitem(sys.modules, "app.track_registry", None)

    result = asyncio.run(vault_routes.get_system_status())

    assert result == {
        "can_deploy": True,
        "total_tracks": 0,
        "active_tracks": [],
    }
