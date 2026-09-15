"""PR5 step 1: point-lookup migration of the key->name helpers.

`main._gov_name_for_key` and `daily_briefing._gov_name_for_key` used to walk the
whole `dao_members.json` monolith to name a single key. They now resolve via the
content-addressed per-key file first (`resolve_key`), falling back to the
monolith only on a miss (plan PUBLIC_KEY_LOOKUP_CACHE_PLAN.md §2.3, PR5 step 1).
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("CONTEXT_REPOS_DIR", tempfile.mkdtemp())
os.environ.setdefault("SESSION_LOG_DIR", tempfile.mkdtemp())

KEY = "MIIB-point-lookup-test-key"

MONOLITH = {
    "version": 2,
    "updated_at": "2026-09-15T00:00:00Z",
    "source": "test",
    "governors": [
        {"name": "Gary Teh", "public_key": KEY, "email": "gary@truesight.me"},
    ],
}


def test_main_helper_uses_point_lookup_first():
    from app import main

    with (
        patch(
            "app.main.resolve_key",
            return_value={"name": "Gary Teh", "is_governor": True},
        ) as pk,
        patch("app.main.load_governors") as mono,
    ):
        assert main._gov_name_for_key(KEY) == "Gary Teh"
    pk.assert_called_once_with(KEY)
    mono.assert_not_called()  # monolith not touched on a point-lookup hit


def test_daily_briefing_helper_uses_point_lookup_first():
    from app import daily_briefing

    with (
        patch(
            "app.daily_briefing.resolve_key",
            return_value={"name": "Gary Teh", "is_governor": True},
        ) as pk,
        patch("app.daily_briefing.load_governors") as mono,
    ):
        assert daily_briefing._gov_name_for_key(KEY) == "Gary Teh"
    pk.assert_called_once_with(KEY)
    mono.assert_not_called()


def test_main_helper_falls_back_to_monolith_on_miss():
    from app import main

    with (
        patch("app.main.resolve_key", return_value=None),
        patch("app.main.load_governors", return_value=MONOLITH) as mono,
    ):
        assert main._gov_name_for_key(KEY) == "Gary Teh"
    mono.assert_called_once()


def test_daily_briefing_helper_falls_back_to_monolith_on_miss():
    from app import daily_briefing

    with (
        patch("app.daily_briefing.resolve_key", return_value=None),
        patch("app.daily_briefing.load_governors", return_value=MONOLITH),
    ):
        assert daily_briefing._gov_name_for_key(KEY) == "Gary Teh"


def test_main_helper_rejects_non_governor_point_lookup():
    """A member key (is_governor False) must NOT be named via the point path."""
    from app import main

    with (
        patch(
            "app.main.resolve_key",
            return_value={"name": "Member Person", "is_governor": False},
        ),
        patch("app.main.load_governors", return_value={"governors": []}),
    ):
        assert main._gov_name_for_key(KEY) is None


def test_main_helper_unknown_key_returns_none():
    from app import main

    with (
        patch("app.main.resolve_key", return_value=None),
        patch("app.main.load_governors", return_value={"governors": []}),
    ):
        assert main._gov_name_for_key("nonexistent-key") is None
