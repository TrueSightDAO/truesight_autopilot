"""PR4c(a) -- Sophia's active-supervision claim/release side-effects.

Hermetic: ``_read_doc``/``_write_doc`` are monkeypatched to an in-memory document,
so no GitHub call is made. The turn hook is covered by resolving a thread via a
monkeypatched ``_plan_for_thread``.
"""

from __future__ import annotations

import json

import pytest

try:
    from app import supervision as sup
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"app.supervision import unavailable: {exc}", allow_module_level=True)


@pytest.fixture()
def store(monkeypatch):
    doc: dict = {"schema_version": 1, "claims": []}
    writes: list[str] = []

    monkeypatch.setattr(sup, "_read_doc", lambda: doc)

    def _fake_write(d, m):
        # Snapshot BEFORE mutating: the module passes the same live dict it read
        # back in, so a naive clear-then-update would wipe the snapshot too.
        snap = json.loads(json.dumps(d))
        writes.append(m)
        doc.clear()
        doc.update(snap)
        return True

    monkeypatch.setattr(sup, "_write_doc", _fake_write)
    return doc, writes


def test_normalize_plan_strips_backticks():
    assert sup.normalize_plan("  `plans/X.md` ") == "plans/X.md"
    assert sup.normalize_plan("") == ""


def test_claim_adds_then_refreshes(store):
    doc, writes = store
    assert sup.claim("plans/X.md", note="first") is True
    assert len(doc["claims"]) == 1
    assert doc["claims"][0] == {
        "plan_file": "plans/X.md",
        "supervisor": sup.SELF_SUPERVISOR,
        "claimed_at": doc["claims"][0]["claimed_at"],
        "note": "first",
    }
    assert sup.claim("plans/X.md", note="again") is True
    assert len(doc["claims"]) == 1  # refreshed, not duplicated
    assert doc["claims"][0]["note"] == "again"
    assert len(writes) == 2


def test_claim_does_not_touch_other_supervisors_entry(store):
    doc, _ = store
    doc["claims"].append(
        {
            "plan_file": "plans/X.md",
            "supervisor": "Envoy (nelanco-claude, supervisor loop)",
            "claimed_at": "2026-09-15T15:43:27Z",
            "note": "envoy's",
        }
    )
    assert sup.claim("plans/X.md") is True
    assert len(doc["claims"]) == 2  # ours added, Envoy's untouched


def test_claim_normalizes_backticked_plan(store):
    doc, _ = store
    assert sup.claim("`plans/X.md`") is True
    assert doc["claims"][0]["plan_file"] == "plans/X.md"


def test_claim_empty_plan_is_noop(store):
    doc, writes = store
    assert sup.claim("") is False
    assert doc["claims"] == [] and writes == []


def test_release_removes_only_ours(store):
    doc, _ = store
    doc["claims"] = [
        {
            "plan_file": "plans/X.md",
            "supervisor": sup.SELF_SUPERVISOR,
            "claimed_at": "t",
        },
        {
            "plan_file": "plans/X.md",
            "supervisor": "Envoy (nelanco-claude, supervisor loop)",
            "claimed_at": "t",
        },
    ]
    assert sup.release("plans/X.md") is True
    assert len(doc["claims"]) == 1
    assert doc["claims"][0]["supervisor"].startswith("Envoy")


def test_release_absent_is_noop(store):
    doc, writes = store
    assert sup.release("plans/X.md") is False
    assert writes == []


def test_claim_for_thread_resolves_then_claims(store, monkeypatch):
    doc, _ = store
    monkeypatch.setattr(sup, "_plan_for_thread", lambda tid: "plans/X.md")
    assert sup.claim_for_thread(123) == "plans/X.md"
    assert doc["claims"][0]["plan_file"] == "plans/X.md"


def test_claim_for_thread_no_plan_returns_none(store, monkeypatch):
    monkeypatch.setattr(sup, "_plan_for_thread", lambda tid: None)
    assert sup.claim_for_thread(123) is None


def test_read_write_failure_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(sup, "_read_doc", boom)
    assert sup.claim("plans/X.md") is False
    monkeypatch.setattr(sup, "_write_doc", boom)
    monkeypatch.setattr(sup, "_read_doc", lambda: {"schema_version": 1, "claims": []})
    assert sup.release("plans/X.md") is False


# --- session key -> thread id -----------------------------------------------


def test_thread_from_session_parses_tg_key():
    from app.main import _thread_from_session

    assert _thread_from_session("tg:-1003919341801:30083") == 30083
    assert _thread_from_session("abc123:tg:-100:5") == 5
    assert _thread_from_session("publickeyonly") is None
    assert _thread_from_session("") is None


# --- claim_for_turn: explicit plan_file wins over thread inference --------------


def test_claim_for_turn_prefers_explicit_plan(monkeypatch):
    import app.supervision as sup

    seen = {}
    monkeypatch.setattr(
        sup,
        "claim",
        lambda plan, *, note="", supervisor=sup.SELF_SUPERVISOR: (
            seen.update(plan=plan, note=note) or True
        ),
    )
    monkeypatch.setattr(
        sup,
        "claim_for_thread",
        lambda tid, *, note="x": (_ for _ in ()).throw(
            AssertionError("thread path must not run")
        ),
    )
    assert sup.claim_for_turn(30083, "plans/X.md") == "plans/X.md"
    assert seen["plan"] == "plans/X.md"
    assert "ping_sophia" in seen["note"]


def test_claim_for_turn_normalizes_backticks(monkeypatch):
    import app.supervision as sup

    monkeypatch.setattr(
        sup, "claim", lambda plan, *, note="", supervisor=sup.SELF_SUPERVISOR: True
    )
    assert sup.claim_for_turn(None, "  `plans/Y.md` ") == "plans/Y.md"


def test_claim_for_turn_falls_back_to_thread(monkeypatch):
    import app.supervision as sup

    monkeypatch.setattr(
        sup,
        "claim_for_thread",
        lambda tid, *, note="x": "plans/Z.md" if tid == 7 else None,
    )
    assert sup.claim_for_turn(7, None) == "plans/Z.md"
    assert sup.claim_for_turn(7, "") == "plans/Z.md"


def test_claim_for_turn_never_raises(monkeypatch):
    import app.supervision as sup

    monkeypatch.setattr(
        sup, "claim", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert sup.claim_for_turn(None, "plans/X.md") is None
