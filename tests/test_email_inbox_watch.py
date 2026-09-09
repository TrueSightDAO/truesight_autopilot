"""Unit tests for the email inbox watch skeleton (app/email_inbox_watch.py)."""

from __future__ import annotations

import json

from app.email_inbox_watch import INBOX_QUERY, EmailInboxWatch


def _ok_results(n: int) -> str:
    return json.dumps(
        {
            "status": "ok",
            "account": "admin",
            "query": INBOX_QUERY,
            "result_count": n,
            "results": [
                {
                    "id": f"m{i}",
                    "thread_id": f"t{i}",
                    "from": "sender@example.com",
                    "subject": f"Subject {i}",
                }
                for i in range(n)
            ],
        }
    )


def test_inbox_query_is_plus_alias_scoped() -> None:
    assert "to:admin+sophia@truesight.me" in INBOX_QUERY
    assert "is:unread" in INBOX_QUERY


def test_search_lazy_imports_gmail_tools() -> None:
    from app.tools.gmail_tools import gmail_search

    watch = EmailInboxWatch()
    assert watch._search() is gmail_search


def test_poll_once_counts_unread(monkeypatch) -> None:
    watch = EmailInboxWatch()

    def fake_search(
        query: str = "", account: str | None = None, max_results: int = 20
    ) -> str:
        return _ok_results(3)

    monkeypatch.setattr(watch, "_search", lambda: fake_search)
    assert watch.poll_once() == 3


def test_poll_once_search_error_returns_zero(monkeypatch) -> None:
    watch = EmailInboxWatch()

    def fake_search(
        query: str = "", account: str | None = None, max_results: int = 20
    ) -> str:
        return json.dumps({"status": "error", "reason": "gmail credentials missing"})

    monkeypatch.setattr(watch, "_search", lambda: fake_search)
    assert watch.poll_once() == 0
