"""Tests for the deploy/push ledger integration (DEPLOY_PUSH_SOP Phase 2)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import deploy_ledger as dl


@pytest.fixture(autouse=True)
def _fake_pat(monkeypatch):
    monkeypatch.setattr(dl.settings, "github_pat", "fake-pat")


def _lease_json(lease: dict) -> dict:
    body = json.dumps(lease).encode("utf-8")
    return {
        "content": base64.b64encode(body).decode("utf-8"),
        "encoding": "base64",
        "sha": "abc",
    }


def _old_lease(now: datetime) -> dict:
    started = (now - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": "L-EXPIRED",
        "agent": "sophia",
        "target_type": "clasp",
        "target_id": "1AbC",
        "started_utc": started,
        "ttl_minutes": 30,
        "status": "open",
    }


def _fresh_lease(now: datetime) -> dict:
    started = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": "L-FRESH",
        "agent": "envoy",
        "target_type": "clasp",
        "target_id": "1AbC",
        "started_utc": started,
        "ttl_minutes": 30,
        "status": "open",
    }


def test_check_lease_clear(monkeypatch):
    monkeypatch.setattr(dl, "_read_dir", lambda path: [])
    out = dl.check_lease("clasp", "1AbC")
    assert out["status"] == "clear"


def test_check_lease_ignores_expired(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        dl, "_read_dir", lambda path: [{"type": "file", "name": "L-EXPIRED.json"}]
    )
    monkeypatch.setattr(
        dl, "_read_file", lambda path: _old_lease(now) if "L-EXPIRED" in path else None
    )
    out = dl.check_lease("clasp", "1AbC")
    assert out["status"] == "clear"


def test_check_lease_blocks_fresh(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        dl, "_read_dir", lambda path: [{"type": "file", "name": "L-FRESH.json"}]
    )
    monkeypatch.setattr(
        dl, "_read_file", lambda path: _fresh_lease(now) if "L-FRESH" in path else None
    )
    out = dl.check_lease("clasp", "1AbC")
    assert out["status"] == "blocked"
    assert out["leases"][0]["lease_id"] == "L-FRESH"


def test_check_lease_scoped_to_target(monkeypatch):
    now = datetime.now(timezone.utc)
    lease = _fresh_lease(now)
    lease["target_id"] = "OtherScript"
    monkeypatch.setattr(
        dl, "_read_dir", lambda path: [{"type": "file", "name": "L-FRESH.json"}]
    )
    monkeypatch.setattr(dl, "_read_file", lambda path: lease)
    out = dl.check_lease("clasp", "1AbC")
    assert out["status"] == "clear"  # lease is on a different target


def test_check_lease_fails_open_without_pat(monkeypatch):
    monkeypatch.setattr(dl.settings, "github_pat", "")
    out = dl.check_lease("clasp", "1AbC")
    assert out["status"] == "error"


def test_acquire_and_close_lease(monkeypatch):
    monkeypatch.setattr(dl, "_read_dir", lambda path: [])
    written = {}
    monkeypatch.setattr(
        dl,
        "_put_file",
        lambda path, content, message: (
            written.update({path: content}) or {"status": "success"}
        ),
    )
    acq = dl.acquire_lease("clasp", "1AbC", "clasp push --force")
    assert acq["status"] == "success"
    assert acq["lease_id"].startswith("L-")

    monkeypatch.setattr(dl, "_delete_file", lambda path, message: {"status": "success"})
    closed = dl.close_lease(acq["lease_id"])
    assert closed["status"] == "success"


def test_append_record_requires_known_agent():
    out = dl.append_deploy_record(
        agent="mallory",
        target_type="clasp",
        target_id="1AbC",
        action="x",
        result="success",
        evidence_url="https://example.com",
    )
    assert out["status"] == "error"
    assert "agent" in out["error"]


def test_append_record_requires_evidence_on_success():
    out = dl.append_deploy_record(
        agent="sophia",
        target_type="clasp",
        target_id="1AbC",
        action="x",
        result="success",
        evidence_url="",
    )
    assert out["status"] == "error"
    assert "evidence_url" in out["error"]


def test_append_record_writes_md_json_and_rebuilds_feed(monkeypatch):
    written = {}
    monkeypatch.setattr(
        dl,
        "_put_file",
        lambda path, content, message: (
            written.update({path: content}) or {"status": "success"}
        ),
    )
    monkeypatch.setattr(dl, "_read_dir", lambda path: [])
    monkeypatch.setattr(dl, "_read_file", lambda path: None)
    out = dl.append_deploy_record(
        agent="sophia",
        target_type="clasp",
        target_id="1AbC",
        action="clasp push --force",
        result="success",
        evidence_url="https://github.com/TrueSightDAO/tokenomics/tree/main/google_app_scripts/1AbC",
    )
    assert out["status"] == "success"
    assert any(p.endswith(".md") for p in written)
    assert any(p.endswith(".json") for p in written)
    assert any("manifest.json" in p for p in written)
