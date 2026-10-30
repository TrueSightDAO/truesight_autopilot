"""A governor's plain-text reply must consume the resume-awaiting entry.

The bug these cover: ``mark_resume_awaiting`` recorded the prompt's message id,
and only ``lookup()`` (reached from the *reaction* path) popped it. A governor
answering with text went through ``handle_message``, which never looks messages
up by id -- so the entry survived to its TTL and the loop kept nagging about a
prompt that had already been answered. ``consume_thread`` / ``consume_channel``
close that gap by clearing at the dispatch seam.
"""

from __future__ import annotations

import threading

import pytest

import app.discord_resume_registry as drr
import app.resume_registry as rr


@pytest.fixture()
def telegram_reg(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "_PATH", tmp_path / "_resume_awaiting.json")
    monkeypatch.setattr(rr, "_lock", threading.Lock())
    return rr


@pytest.fixture()
def discord_reg(tmp_path, monkeypatch):
    monkeypatch.setattr(drr, "_PATH", tmp_path / "_discord_resume_awaiting.json")
    monkeypatch.setattr(drr, "_lock", threading.Lock())
    return drr


def test_consume_thread_clears_entries_for_that_thread(telegram_reg):
    telegram_reg.mark_resume_awaiting(111, 9001, "unit one")
    telegram_reg.mark_resume_awaiting(222, 9001, "unit two")
    assert telegram_reg.consume_thread(9001) == 2
    # and the prompts are no longer awaiting
    assert telegram_reg.lookup(111) is None
    assert telegram_reg.lookup(222) is None


def test_consume_thread_leaves_other_threads_alone(telegram_reg):
    telegram_reg.mark_resume_awaiting(111, 9001, "mine")
    telegram_reg.mark_resume_awaiting(333, 9002, "someone else's")
    assert telegram_reg.consume_thread(9001) == 1
    # untouched thread still resolves
    assert telegram_reg.lookup(333) is not None


def test_consume_thread_is_idempotent(telegram_reg):
    telegram_reg.mark_resume_awaiting(111, 9001, "only one")
    assert telegram_reg.consume_thread(9001) == 1
    assert telegram_reg.consume_thread(9001) == 0


def test_consume_thread_on_empty_registry_is_a_noop(telegram_reg):
    assert telegram_reg.consume_thread(12345) == 0


def test_consume_thread_accepts_str_thread_id(telegram_reg):
    """Adapters pass thread ids straight off the wire, which arrive as str."""
    telegram_reg.mark_resume_awaiting(111, 9001, "wire type")
    assert telegram_reg.consume_thread("9001") == 1


def test_consume_channel_clears_entries_for_that_channel(discord_reg):
    discord_reg.mark_resume_awaiting(111, 555, "a")
    discord_reg.mark_resume_awaiting(222, 555, "b")
    assert discord_reg.consume_channel(555) == 2
    assert discord_reg.lookup(111) is None


def test_consume_channel_leaves_other_channels_alone(discord_reg):
    discord_reg.mark_resume_awaiting(111, 555, "mine")
    discord_reg.mark_resume_awaiting(333, 666, "other")
    assert discord_reg.consume_channel(555) == 1
    assert discord_reg.lookup(333) is not None


def test_consume_channel_accepts_str_channel_id(discord_reg):
    discord_reg.mark_resume_awaiting(111, 555, "wire type")
    assert discord_reg.consume_channel("555") == 1


def test_consume_channel_is_idempotent(discord_reg):
    discord_reg.mark_resume_awaiting(111, 555, "once")
    assert discord_reg.consume_channel(555) == 1
    assert discord_reg.consume_channel(555) == 0


def test_consume_does_not_break_reaction_lookup_path(telegram_reg):
    """The reaction path must still be able to pop a *different* entry."""
    telegram_reg.mark_resume_awaiting(111, 9001, "answered by text")
    telegram_reg.mark_resume_awaiting(444, 9002, "answered by reaction")
    telegram_reg.consume_thread(9001)
    popped = telegram_reg.lookup(444)
    assert popped is not None
    assert popped.get("text") == "answered by reaction"
