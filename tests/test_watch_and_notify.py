"""watch-and-notify: poller state logic + the tools that launch it.

Makes Sophia's 'I'll let you know when it's done' real — proven gap 2026-06-10
(an AMI reached `available` and she never reported). Pure-unit only; no boto3,
network, or real subprocess.
"""
from __future__ import annotations

import pytest

from app import watch_runner as wr


# ── poller classification (the heart: when is an op done/failed/pending) ──────

def test_ami_states():
    spec_resp = lambda st: {"Images": [{"State": st}]}
    assert wr._classify_aws("ami", spec_resp("available")) == ("done", "available")
    assert wr._classify_aws("ami", spec_resp("pending")) == ("pending", "pending")
    assert wr._classify_aws("ami", spec_resp("failed")) == ("failed", "failed")


def test_not_found_is_pending_not_done():
    # Eventual consistency: a describe that returns nothing yet must NOT be 'done'.
    assert wr._classify_aws("ami", {"Images": []}) == ("pending", None)
    assert wr._classify_aws("snapshot", {"status": "error", "reason": "x"}) == ("pending", None)


def test_snapshot_and_volume_terminal_states():
    assert wr._classify_aws("snapshot", {"Snapshots": [{"State": "completed"}]}) == ("done", "completed")
    assert wr._classify_aws("snapshot", {"Snapshots": [{"State": "pending"}]})[0] == "pending"
    assert wr._classify_aws("volume", {"Volumes": [{"State": "available"}]}) == ("done", "available")


def test_instance_nested_state_extraction():
    resp = {"Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]}
    assert wr._classify_aws("instance_running", resp) == ("done", "running")
    booting = {"Reservations": [{"Instances": [{"State": {"Name": "pending"}}]}]}
    assert wr._classify_aws("instance_running", booting)[0] == "pending"
    assert wr._classify_aws("instance_running", {"Reservations": []}) == ("pending", None)


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        wr._resource_spec("nope")


def test_probe_unwraps_aws_query_envelope(monkeypatch):
    """aws_query wraps the boto3 result as {'status':'ok','response':{...}}; the
    probe must unwrap it (regression: top-level lookup made every AMI look pending
    so the watcher would falsely report 'still not done')."""
    import json as _json
    import app.tools.aws_tools as awt
    monkeypatch.setattr(awt, "aws_query", lambda **kw: _json.dumps(
        {"status": "ok", "account": "nelanco", "response": {"Images": [{"State": "available"}]}}))
    assert wr._probe_aws("ami", "ami-1", "nelanco", "us-east-1") == ("done", "available")

    monkeypatch.setattr(awt, "aws_query", lambda **kw: _json.dumps({"status": "error", "reason": "boom"}))
    assert wr._probe_aws("ami", "ami-1", "nelanco", "us-east-1") == ("pending", None)


# ── tools: session parsing + detached launch ─────────────────────────────────

def _import_watch_tools():
    try:
        from app.tools import watch_tools
        return watch_tools
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"watch_tools import unavailable: {exc}")


def test_session_parse():
    wt = _import_watch_tools()
    assert wt._chat_thread_from_session("tg:-1003919341801:780") == ("-1003919341801", "780")
    assert wt._chat_thread_from_session("tg:-100:0") == ("-100", None)   # bare topic
    assert wt._chat_thread_from_session("pubkeyabc:websess") == (None, None)  # DApp session
    assert wt._chat_thread_from_session(None) == (None, None)


def test_watch_aws_resource_launches_and_confirms(monkeypatch):
    wt = _import_watch_tools()
    launched = {}
    monkeypatch.setattr(wt, "_launch", lambda argv: launched.setdefault("argv", argv))

    out = wt.watch_aws_resource(
        {"resource_kind": "ami", "resource_id": "ami-05da693e385f7585a",
         "account": "nelanco", "region": "us-east-1", "label": "getdata-cache AMI"},
        {"session_id": "tg:-1003919341801:780"},
    )
    import json
    res = json.loads(out)
    assert res["status"] == "watching"
    argv = launched["argv"]
    assert "--kind" in argv and argv[argv.index("--kind") + 1] == "ami"
    assert "ami-05da693e385f7585a" in argv
    assert argv[argv.index("--chat-id") + 1] == "-1003919341801"
    assert argv[argv.index("--thread-id") + 1] == "780"


def test_watch_rejects_non_telegram_session(monkeypatch):
    wt = _import_watch_tools()
    monkeypatch.setattr(wt, "_launch", lambda argv: pytest.fail("must not launch"))
    import json
    out = json.loads(wt.watch_aws_resource(
        {"resource_kind": "ami", "resource_id": "ami-1", "account": "nelanco"},
        {"session_id": "pubkey:web"},
    ))
    assert out["status"] == "error"


def test_watch_aws_rejects_bad_kind(monkeypatch):
    wt = _import_watch_tools()
    monkeypatch.setattr(wt, "_launch", lambda argv: pytest.fail("must not launch"))
    import json
    out = json.loads(wt.watch_aws_resource(
        {"resource_kind": "database", "resource_id": "x", "account": "nelanco"},
        {"session_id": "tg:-100:5"},
    ))
    assert out["status"] == "error"
