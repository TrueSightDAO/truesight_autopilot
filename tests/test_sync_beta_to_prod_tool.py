"""sync_beta_to_prod tool spec must stay in sync with settings.prod_repos.

Regression guard for the single-source-of-truth rule: the tool's enum (and its
human-facing description) must derive from ``settings.prod_repos``, never be a
hand-maintained duplicate. A future prod repo added to config must be callable
through the tool with zero extra edits.
"""

from __future__ import annotations

from app.config import settings
from app.tools.sync_beta_to_prod import TOOL_SPEC


def test_enum_matches_settings_prod_repos():
    """The prod_repo enum must equal the config keys (sorted), exactly."""
    enum = TOOL_SPEC.parameters["properties"]["prod_repo"]["enum"]
    assert enum == sorted(settings.prod_repos)


def test_description_lists_all_prod_repos():
    """The tool description must mention every configured prod repo, so the
    operator-facing contract can't drift from config."""
    desc = TOOL_SPEC.description
    for repo in settings.prod_repos:
        assert repo in desc


def test_enum_has_no_duplicates():
    """Sanity: the derived enum is a clean, deduplicated sorted list."""
    enum = TOOL_SPEC.parameters["properties"]["prod_repo"]["enum"]
    assert len(enum) == len(set(enum))
