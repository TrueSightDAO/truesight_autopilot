"""Guard: the Redis client must be a RUNTIME dependency, not a dev-only one.

``app/redis_claim.py`` imports ``redis`` lazily inside ``get_client()`` so the
module itself always imports cleanly. That laziness hid a real deployment
defect: PR1 of the mark-and-claim plan pinned ``redis`` in
``requirements-dev.txt`` only, so the code deployed and passed CI, then raised
``ModuleNotFoundError`` the first time a lock was actually taken on the live
host. This test pins the dependency to the runtime file so a later refactor
cannot quietly move it back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _package_names(filename: str) -> set[str]:
    """Normalised package names from a requirements file (comments/blanks stripped)."""
    path = REPO_ROOT / filename
    if not path.is_file():
        pytest.skip(f"{filename} not present in this checkout")
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # strip any version specifier / extras / environment marker
        for sep in ("[", "=", ">", "<", "!", "~", ";", " "):
            line = line.split(sep, 1)[0]
        if line:
            names.add(line.strip().lower())
    return names


def test_redis_is_a_runtime_dependency() -> None:
    assert "redis" in _package_names("requirements.txt"), (
        "redis must be listed in requirements.txt: app/redis_claim.get_client() "
        "imports it at call time, so a dev-only pin deploys silently broken"
    )
