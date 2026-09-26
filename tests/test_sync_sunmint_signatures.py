"""Unit tests for scripts/sync_sunmint_signatures.py (no network)."""

import sys

import pytest

pytest.importorskip(
    "gspread",
    reason="gspread not installed in CI deps; quarantine until requirements include it",
)

sys.path.insert(0, "scripts")

from scripts.sync_sunmint_signatures import (  # noqa: E402
    _has_cpf_pii,
    _ledger_files,
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

CPF_EXTRA_PK = _PK + "C" * 200

CPF_SAMPLE = (
    "[CONTRIBUTION EVENT]\n"
    "- PIX key type: CPF\n"
    "- PIX key: 079.021.202-19\n"
    "--------\n\n"
    "My Digital Signature: " + CPF_EXTRA_PK + "\n\n"
    "Request Transaction ID: " + SIG + "\n"
)

CNPJ_SAMPLE = (
    "[CONTRIBUTION EVENT]\n"
    "- Description: supplier Black King, CNPJ 12.345.678/0001-90\n"
    "--------\n\n"
    "My Digital Signature: " + CPF_EXTRA_PK + "\n\n"
    "Request Transaction ID: " + SIG + "\n"
)


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

PAYOUT_SAMPLE = (
    "[PAYOUT REGISTRATION]\n"
    "- PIX Key Type: cpf\n"
    "- PIX Key: 123.456.789-01\n"
    "- Program: crf-anapu\n"
    "--------\n\n"
    "My Digital Signature: " + PLANT_PK + "\n\n"
    "Request Transaction ID: " + SIG + "\n"
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


@pytest.mark.xfail(
    reason="parse_event includes emails since publish-time EMAIL_RE fail-closed (sync_sunmint_signatures.py:227); exclusion moved to publish gate"
)
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


def test_payout_registration_hard_excluded_under_allow_pii():
    """[PAYOUT REGISTRATION] carries a raw PIX -> excluded even under --allow-pii."""
    chat = [
        [
            "4690",
            "",
            "",
            "900",
            "student",
            "",
            PAYOUT_SAMPLE,
            "",
            "0",
            "",
            "",
            "20260917",
        ],
    ]
    out = build_signatures(chat, {}, {}, allow_pii=True)
    assert out["count"] == 0
    assert "900" in out["excluded_pii_events"]
    assert "900" not in out["events"]
    assert "900" not in out["other_signed"]
    # The raw PIX must not be stashed in the exclusion report either.
    assert "123.456.789-01" not in str(out["excluded_pii_events"])


def test_ledger_files_never_emits_hard_excluded_marker():
    sigs = {
        "events": {
            "900": {"event_type": "[PAYOUT REGISTRATION]", "signed_text": PAYOUT_SAMPLE}
        }
    }
    files = _ledger_files(sigs, {"items": []})
    assert not any("payout" in p for p in files)


def _ev(i, txid, marker="[TREE PLANTING EVENT]"):
    return {
        "event_type": marker,
        "telegram_message_id": i,
        "signature": txid,
        "signed_payload": "p-" + txid,
        "signed_text": marker + "\nRequest Transaction ID: " + txid + "\n",
        "submitted_at": "2026-09-26",
        "contributor_name": "Gary",
    }


def test_txid_key_is_64_hex_and_stable():
    import hashlib

    from scripts.sync_sunmint_signatures import _txid_key

    txid = "x" * 300 + "/+="
    k = _txid_key(txid)
    assert len(k) == 64
    assert all(c in "0123456789abcdef" for c in k)
    assert k == hashlib.sha256(txid.encode()).hexdigest()
    assert _txid_key(txid) == _txid_key(txid)  # deterministic


def test_txid_mirror_created_and_message_id_alias_preserved():
    from scripts.sync_sunmint_signatures import _ledger_files, _txid_key

    txid = "SIGA" * 80
    sigs = {"events": {"171": _ev("171", txid), "172": _ev("172", txid)}}
    files = _ledger_files(sigs, {"items": []})
    folder = "tree_planting"
    # (4) BOTH names written: message-id alias files + the txid mirror.
    assert f"{folder}/171.json" in files
    assert f"{folder}/172.json" in files
    mirror = f"{folder}/{_txid_key(txid)}.json"
    assert mirror in files
    # mirror carries the txid as a JSON field so it can be round-tripped.
    assert files[mirror]["request_transaction_id"] == txid
    # (2)/(3) two message ids, one txid -> exactly ONE mirror (collapse),
    # canonical = earliest message id (171).
    assert len([p for p in files if p.split("/")[-1] == _txid_key(txid) + ".json"]) == 1
    idx = files[f"{folder}/index.json"]
    assert idx["txid_count"] == 1
    assert idx["txids"][_txid_key(txid)]["telegram_message_id"] == "171"
    # alias files carry NO request_transaction_id field -> per-event schema
    # stays byte-identical to before (no drift in existing published files).
    assert "request_transaction_id" not in files[f"{folder}/171.json"]


def test_txid_root_index_counts():
    from scripts.sync_sunmint_signatures import _ledger_files

    sigs = {
        "events": {
            "171": _ev("171", "T1" * 100),
            "172": _ev("172", "T1" * 100),
            "180": _ev("180", "T2" * 100),
        }
    }
    root = _ledger_files(sigs, {"items": []})["index.json"]
    assert root["total_txid_count"] == 2
    assert root["txid_mirror_count"] == 2
    assert root["txid_dup_groups"] == 1
    assert root["txid_extra_files_collapsed"] == 1
    assert root["txid_mirror_collisions"] == 0


def test_events_without_txid_get_no_mirror():
    from scripts.sync_sunmint_signatures import _ledger_files

    ev = _ev("200", "")
    sigs = {"events": {"200": ev}}
    files = _ledger_files(sigs, {"items": []})
    assert "tree_planting/200.json" in files
    # no txid -> no mirror, and the alias file is untouched.
    assert "request_transaction_id" not in files["tree_planting/200.json"]
    assert files["tree_planting/index.json"]["txid_count"] == 0


def test_cpf_events_excluded_under_allow_pii():
    """A raw CPF excludes the event, even under --allow-pii."""
    chat = [
        ["4690", "", "", "901", "employee", "", CPF_SAMPLE, "", "0", "", "", "20260926"]
    ]
    out = build_signatures(chat, {}, {}, allow_pii=True)
    assert out["count"] == 0
    assert "901" in out["excluded_pii_events"]
    assert "901" not in out["events"]
    assert "079.021.202-19" not in str(out["excluded_pii_events"])


def test_cnpj_events_are_published():
    """CNPJ is a business id, not personal PII -- it must NOT be excluded."""
    chat = [
        [
            "4691",
            "",
            "",
            "902",
            "accountant",
            "",
            CNPJ_SAMPLE,
            "",
            "0",
            "",
            "",
            "20260926",
        ]
    ]
    out = build_signatures(chat, {}, {}, allow_pii=True)
    assert out["count"] == 1
    assert "902" in out["events"]


def test_has_cpf_pii_matches_punctuated_and_labelled():
    assert _has_cpf_pii("- PIX key: 079.021.202-19")
    assert _has_cpf_pii("- PIX key: 07902120219")


def test_has_cpf_pii_ignores_bare_ids_and_cnpj():
    assert not _has_cpf_pii("- msg id: 4690272682")
    assert not _has_cpf_pii("(Black King, CNPJ 12.345.678/0001-90)")
