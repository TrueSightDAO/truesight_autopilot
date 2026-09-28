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


def _plant_row(msg_id, status_date, name="A"):
    return [
        "1",
        "",
        "",
        msg_id,
        "garyjob",
        "",
        PLANT_SAMPLE,
        "",
        "0",
        "",
        "",
        status_date,
    ]


def test_folder_index_orders_events_chronologically():
    """Ordering lives in the index (ordered_by + events_ordered), not the path."""
    chat = [
        _plant_row("300", "20260715"),  # middle
        _plant_row("100", "20250101"),  # earliest
        _plant_row("200", "20260601"),  # latest
    ]
    sigs = build_signatures(chat, {}, {})
    files = _ledger_files(sigs, {"items": []})
    idx = files["tree_planting/index.json"]

    # ordered_by names the sort key; the vector is chronological (ISO dates sort
    # lexicographically == chronologically).
    assert idx["ordered_by"] == "submitted_at"
    assert idx["events_ordered"] == ["100", "200", "300"]

    # The array is a faithful enumeration of the events dict -- nothing dropped.
    assert set(idx["events_ordered"]) == set(idx["events"].keys())
    assert len(idx["events_ordered"]) == idx["count"]

    # Each id resolves through the dict to its metadata (walk contract).
    assert idx["events"]["100"]["submitted_at"] == "2025-01-01"
    assert idx["events"]["300"]["submitted_at"] == "2026-07-15"


def test_events_dict_iteration_order_is_chronological():
    """The `events` dict is emitted in chronological order too (stable ties)."""
    chat = [
        _plant_row("500", "20260901"),
        _plant_row("400", "20250101"),
    ]
    sigs = build_signatures(chat, {}, {})
    files = _ledger_files(sigs, {"items": []})
    idx = files["tree_planting/index.json"]
    assert list(idx["events"].keys()) == ["400", "500"]


def test_ordering_ties_broken_by_id_deterministically():
    """Same submitted_at -> stable, total order by message id (no ambiguity)."""
    chat = [
        _plant_row("900", "20260601"),
        _plant_row("800", "20260601"),
        _plant_row("700", "20260601"),
    ]
    sigs = build_signatures(chat, {}, {})
    files = _ledger_files(sigs, {"items": []})
    idx = files["tree_planting/index.json"]
    assert idx["events_ordered"] == ["700", "800", "900"]


def test_paths_unchanged_by_ordering_feature():
    """Ordering is index-only -- event file paths must NOT gain a date prefix."""
    chat = [_plant_row("123", "20260601")]
    sigs = build_signatures(chat, {}, {})
    files = _ledger_files(sigs, {"items": []})
    assert "tree_planting/123.json" in files
    assert not any(p.split("/")[-1][:8].isdigit() for p in files if p.endswith(".json"))


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


def test_upload_retries_409_with_fresh_sha(monkeypatch):
    """A 409 (stale sha) is retried once against the fresh remote sha."""
    import io
    import json
    import urllib.error

    from scripts import sync_sunmint_signatures as m

    calls = {"get": 0, "put": 0}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        if req.get_method() == "GET":
            calls["get"] += 1
            sha = "stale" if calls["get"] == 1 else "fresh"
            return _Resp(json.dumps({"sha": sha}).encode())
        calls["put"] += 1
        if calls["put"] == 1:
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)
        return _Resp(json.dumps({"commit": {"sha": "abc"}}).encode())

    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    assert m._upload("whatever/path.json", {"a": 1}) is True
    assert calls == {"get": 2, "put": 2}


def test_upload_409_then_sha_match_skips(monkeypatch):
    """If after a 409 the content already matches, skip instead of erroring."""
    import io
    import json
    import urllib.error

    from scripts import sync_sunmint_signatures as m

    calls = {"get": 0, "put": 0}
    target = {"a": 1}
    body = json.dumps(target, indent=2, ensure_ascii=False) + "\n"
    local_sha = m._git_blob_sha(body)

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        if req.get_method() == "GET":
            calls["get"] += 1
            sha = "stale" if calls["get"] == 1 else local_sha
            return _Resp(json.dumps({"sha": sha}).encode())
        calls["put"] += 1
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    assert m._upload("whatever/path.json", target) is False
    assert calls == {"get": 2, "put": 1}


def test_run_lock_is_single_flight(tmp_path, monkeypatch):
    """The second concurrent acquisition fails while the first holds the lock."""
    from scripts import sync_sunmint_signatures as m

    monkeypatch.setattr(m, "_LOCK_PATH", str(tmp_path / ".lock"))
    fh1 = m._acquire_run_lock()
    assert fh1 is not None
    assert m._acquire_run_lock() is None  # already held
    fh1.close()
    fh2 = m._acquire_run_lock()
    assert fh2 is not None  # released after close
    fh2.close()


def test_push_ledger_publishes_summary_files_first(monkeypatch, tmp_path):
    """Root + per-folder index.json land BEFORE any per-event file."""
    from scripts import sync_sunmint_signatures as m

    order = []
    monkeypatch.setattr(m, "_upload", lambda p, payload: (order.append(p), True)[1])
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    files = {
        "asset_receipt_event/aaa.json": {"x": 1},
        "asset_receipt_event/index.json": {"x": 2},
        "contribution_event/zzz.json": {"x": 3},
        "index.json": {"x": 4},
    }
    m._push_ledger(files, max_uploads=250, cursor_path=str(tmp_path / "cur"))
    assert order[:2] == ["asset_receipt_event/index.json", "index.json"]
    assert set(order[2:]) == {
        "asset_receipt_event/aaa.json",
        "contribution_event/zzz.json",
    }


def test_push_ledger_cap_counts_events_not_summaries(monkeypatch, tmp_path):
    """The per-run cap applies to event files only; summaries are uncapped."""
    from scripts import sync_sunmint_signatures as m

    order = []

    def fake_upload(p, payload):
        order.append(p)
        m._PUSHED_THIS_RUN.append(p)  # mirror real _upload bookkeeping
        return True

    monkeypatch.setattr(m, "_upload", fake_upload)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    files = {
        "a/1.json": {},
        "b/2.json": {},
        "c/3.json": {},
        "index.json": {},
    }
    cur = tmp_path / "cur"
    m._push_ledger(files, max_uploads=2, cursor_path=str(cur))
    assert "index.json" in order  # summary pushed despite cap
    events = [p for p in order if not p.endswith("index.json")]
    assert len(events) == 2
    # cursor saved so the 3rd event resumes next run
    assert cur.read_text().strip() == "b/2.json"
