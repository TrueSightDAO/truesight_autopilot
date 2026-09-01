"""Unit tests for scripts/sync_sunmint_signatures.py (no network)."""

import sys

import pytest

pytest.importorskip("gspread", reason="gspread not installed in CI deps; quarantine until requirements include it")

sys.path.insert(0, "scripts")

from scripts.sync_sunmint_signatures import (  # noqa: E402
    _scan,
    build_measurements,
    build_signatures,
    parse_event,
)

# Real RSA-2048 SPKI prefix so mocked events pass the _SPKI_PREFIX gate
_PK = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"
PLANT_PK = _PK + "A" * 200
GROW_PK = _PK + "B" * 200
SIG = "x" * 300

PLANT_SAMPLE = (
    "[TREE PLANTING EVENT]\n"
    "- Latitude: 44.560058\n"
    "- Longitude: -123.262181\n"
    "--------\n\n"
    "My Digital Signature: " + PLANT_PK + "\n\n"
    "Request Transaction ID: " + SIG + "\n"
)

GROWTH_SAMPLE = (
    "[TREE GROWTH MONITORING EVENT]\n"
    "- Tree ID: 469027268\n"
    "- Species: unknown\n"
    "- DBH (cm): 12.5\n"
    "- Latitude: 44.560058\n"
    "- Longitude: -123.262181\n"
    "- Measurement Time: 2026-08-29T14:39:52.160Z\n"
    "--------\n\n"
    "My Digital Signature: " + GROW_PK + "\n\n"
    "Request Transaction ID: " + SIG + "\n"
)

EMAIL_SAMPLE = (
    "[EMAIL VERIFICATION EVENT]\n"
    "- Email: farmer@example.com\n"
    "--------\n\n"
    "My Digital Signature: MIIB_PUBKEY_EMAIL\n\n"
    "Request Transaction ID: TXN_HASH_EMAIL\n"
)


def test_parse_event_planting():
    parsed = parse_event(PLANT_SAMPLE)
    assert parsed == {
        "marker": "[TREE PLANTING EVENT]",
        "public_key": PLANT_PK,
        "signature": SIG,
        "payload": (
            "[TREE PLANTING EVENT]\n"
            "- Latitude: 44.560058\n"
            "- Longitude: -123.262181\n"
            "--------"
        ),
    }


def test_parse_event_growth():
    parsed = parse_event(GROWTH_SAMPLE)
    assert parsed["marker"] == "[TREE GROWTH MONITORING EVENT]"
    assert parsed["public_key"] == GROW_PK


def test_email_events_excluded():
    assert parse_event(EMAIL_SAMPLE) is None


def test_build_signatures_keyed_by_msg_id():
    chat = [
        [
            "469027268",
            "",
            "",
            "171",
            "garyjob",
            "",
            PLANT_SAMPLE,
            "",
            "0",
            "",
            "",
            "20250711",
        ],
        [
            "999",
            "",
            "",
            "172",
            "farmer",
            "",
            GROWTH_SAMPLE,
            "",
            "0",
            "",
            "",
            "20260829",
        ],
    ]
    plant = {"171": {"submitted_name": "Gary Teh", "linked_qr": "", "tree_id": ""}}
    growth = {"172": {"tree_id": "469027268"}}
    out = build_signatures(chat, plant, growth)
    assert out["count"] == 2
    assert "171" in out["events"]
    assert out["events"]["171"]["event_type"] == "[TREE PLANTING EVENT]"
    assert out["events"]["171"]["contributor_name"] == "Gary Teh"
    assert out["events"]["172"]["source_tab"].startswith("Tree Growth Measurements")
    assert out["events"]["172"]["linked_tree_id"] == "469027268"


def test_build_measurements_joins_signature():
    growth = [
        [
            "1",
            "172",
            "469027268",
            "unknown",
            "12.5",
            "",
            "",
            "44.5",
            "-123.2",
            "2026-08-29T14:39:52Z",
            "https://raw.../closeup.jpg",
            "https://raw.../context.jpg",
            "https://github.com/.../commit/abc",
            "sha256abc",
            GROW_PK,
            "Gary Teh",
            "LIVE",
            "2026-08-29T15:00:00Z",
        ],
    ]
    chat = {"172": {"signature": SIG, "signed_text": GROWTH_SAMPLE}}
    out = build_measurements(growth, chat)
    assert out["count"] == 1
    item = out["items"][0]
    assert item["tree_id"] == "469027268"
    assert item["signature"] == SIG
    assert item["farmer_public_key"] == GROW_PK


def test_pii_scan_blocks_email():
    import pytest

    with pytest.raises(SystemExit):
        _scan({"events": {"1": {"signed_text": "contact farmer@example.com now"}}})
