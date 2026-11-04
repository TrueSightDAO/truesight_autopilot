"""Tests for the sidebar layout overlay + the /session truncation cap.

The layout overlay is what makes sidebar drag-and-drop possible for BUILT-IN
channels/categories: those live in each deployment's config.json and cannot be
mutated at runtime, so placement is recorded next to ``retired`` in the Redis
registry instead.
"""

from __future__ import annotations

import pytest

from app import channel_registry


class FakeRedis:
    """Minimal in-memory stand-in for the Redis client the registry uses."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(channel_registry.redis_claim, "get_client", lambda: client)
    return client


def test_fresh_layout_is_empty(fake_redis):
    layout = channel_registry.get_layout()
    assert layout == {"category_order": [], "channels": {}, "collapsed": []}


def test_move_channel_between_categories(fake_redis):
    channel_registry.update_layout(
        {"move": {"channel_id": "ch_abc", "category_name": "Growth", "position": 2}}
    )
    layout = channel_registry.get_layout()
    assert layout["channels"]["ch_abc"] == {
        "category_name": "Growth",
        "position": 2,
    }


def test_move_channel_without_position_keeps_placement(fake_redis):
    channel_registry.update_layout({"move": {"channel_id": "ch_x", "position": 3}})
    channel_registry.update_layout(
        {"move": {"channel_id": "ch_x", "category_name": "Ops"}}
    )
    placement = channel_registry.get_layout()["channels"]["ch_x"]
    assert placement == {"position": 3, "category_name": "Ops"}


def test_category_order_and_collapsed_are_replaced_wholesale(fake_redis):
    channel_registry.update_layout(
        {"category_order": ["Ops", "Growth"], "collapsed": ["Ops"]}
    )
    channel_registry.update_layout({"category_order": ["Growth", "Ops"]})
    layout = channel_registry.get_layout()
    assert layout["category_order"] == ["Growth", "Ops"]
    # collapsed untouched by the second payload
    assert layout["collapsed"] == ["Ops"]


def test_unknown_and_malformed_keys_are_ignored(fake_redis):
    channel_registry.update_layout(
        {
            "nonsense": 1,
            "category_order": "not-a-list",
            "collapsed": [None, "", "  "],
            "move": {"channel_id": "", "category_name": "Ops"},
        }
    )
    layout = channel_registry.get_layout()
    assert layout["category_order"] == []
    assert layout["collapsed"] == []
    assert layout["channels"] == {}


def test_layout_survives_other_registry_writes(fake_redis):
    channel_registry.update_layout({"collapsed": ["Legal"]})
    channel_registry.create_channel("Ops", "new-room", "public")
    assert channel_registry.get_layout()["collapsed"] == ["Legal"]


def test_missing_layout_key_is_backfilled(fake_redis):
    """A registry written before this feature existed must still load."""
    fake_redis.set(
        channel_registry.REGISTRY_KEY,
        '{"channels": [], "categories": [], "retired": []}',
    )
    layout = channel_registry.get_layout()
    assert layout == {"category_order": [], "channels": {}, "collapsed": []}
