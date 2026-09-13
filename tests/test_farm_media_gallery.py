"""Unit tests for app.farm_media_gallery (Media Gallery Publisher PR3)."""

from __future__ import annotations

import json

from app import farm_media_gallery as g


# --- caption QA guard -------------------------------------------------------


def test_clean_caption_drops_amara_hallucination():
    assert g.clean_caption("Legendas pela comunidade de Amara.org") == ""
    assert g.clean_caption("Legendas pela comunidade de Amara.org ") == ""
    assert g.clean_caption("Subtitles by the Amara.org community") == ""


def test_clean_caption_collapses_whitespace_and_keeps_real_text():
    assert g.clean_caption("  Este   é o cacau  clonal ") == "Este é o cacau clonal"


def test_clean_caption_drops_empty_and_tiny():
    assert g.clean_caption(None) == ""
    assert g.clean_caption("") == ""
    assert g.clean_caption("   ") == ""
    assert g.clean_caption("ok") == ""  # < 3 chars


def test_clean_caption_soft_boilerplate_only_when_whole_caption():
    # boilerplate alone -> dropped
    assert g.clean_caption("Thanks for watching!") == ""
    assert g.clean_caption("Obrigado por assistir") == ""
    # boilerplate inside real speech -> kept
    kept = "Thanks for watching the drying racks, aqui fermenta o cacau por seis dias"
    assert g.clean_caption(kept) == kept


# --- aspect -----------------------------------------------------------------


def test_aspect_from_dims():
    assert g.aspect_from_dims(1080, 1920) == "portrait"
    assert g.aspect_from_dims(1920, 1080) == "landscape"
    assert g.aspect_from_dims(1000, 1000) == "landscape"
    assert g.aspect_from_dims(None, None) == "landscape"
    assert g.aspect_from_dims("x", "y") == "landscape"


# --- gather from inbox sidecars --------------------------------------------


def _write_sidecar(d, name, **over):
    data = {
        "file": name.replace(".mp4.json", ".MOV"),
        "farm_id": "demo-farm",
        "sha256": "deadbeef",
        "title": "demo",
        "yt_id": None,
        "duration_s": 12.0,
        "captured_at": "2026-09-09T13:35:23+0000",
        "transcription": "cacau na veia",
    }
    data.update(over)
    p = d / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_gather_videos_only_uploaded_and_sorted(tmp_path):
    _write_sidecar(tmp_path, "IMG_0002.mp4.json", yt_id=None)  # not uploaded
    _write_sidecar(tmp_path, "IMG_0001.mp4.json", yt_id="aaa")
    _write_sidecar(tmp_path, "IMG_0003.mp4.json", yt_id="bbb")
    vids = g.gather_videos(tmp_path, probe=False)
    assert [v["yt_id"] for v in vids] == ["aaa", "bbb"]
    assert all(v["aspect"] is None for v in vids)


def test_gather_videos_missing_dir_is_empty(tmp_path):
    assert g.gather_videos(tmp_path / "nope", probe=False) == []


def test_gather_videos_tolerates_bad_json(tmp_path):
    (tmp_path / "broken.mp4.json").write_text("{not json", encoding="utf-8")
    _write_sidecar(tmp_path, "IMG_0001.mp4.json", yt_id="aaa")
    assert [v["yt_id"] for v in g.gather_videos(tmp_path, probe=False)] == ["aaa"]


# --- build ------------------------------------------------------------------


def _v(yt, file, when, text="texto real do campo"):
    return {
        "file": file,
        "yt_id": yt,
        "duration_s": 10.0,
        "captured_at": when,
        "transcription": text,
        "aspect": "portrait",
    }


def test_build_gallery_orders_by_capture_time_then_file():
    vids = [
        _v("c", "IMG_9.MOV", "2026-09-09T13:00:00+0000"),
        _v("a", "IMG_2.MOV", "2026-09-09T12:00:00+0000"),
        _v("b", "IMG_3.MOV", "2026-09-09T12:00:00+0000"),
    ]
    out = g.build_gallery("demo-farm", vids, site_name="Demo Farm")
    assert [e["videoId"] for e in out["gallery"]] == ["a", "b", "c"]


def test_build_gallery_entry_shape_and_defaults():
    out = g.build_gallery(
        "demo-farm",
        [_v("a", "IMG_2.MOV", "2026-09-09T12:00:00+0000")],
        site_name="Demo Farm",
    )
    assert out["schemaVersion"] == 1
    assert out["collection_id"] == "demo-farm"
    e = out["gallery"][0]
    assert e["type"] == "youtube"
    assert e["videoId"] == "a"
    assert e["title"] == "Demo Farm — IMG_2 (10 s)"
    assert e["caption"] == "texto real do campo"
    assert e["aspect"] == "portrait"
    assert "generated" not in out


def test_build_gallery_hallucinated_transcript_falls_back_to_boilerplate():
    out = g.build_gallery(
        "demo-farm",
        [
            _v(
                "a",
                "IMG_9509.MOV",
                "2026-09-09T12:00:00+0000",
                text="Legendas pela comunidade de Amara.org",
            )
        ],
        site_name="Sítio Cacau na Veia",
        caption_prefix="Site walk 9 September 2026 · Pacajá, Pará",
    )
    cap = out["gallery"][0]["caption"]
    assert "Amara" not in cap
    assert cap == "Site walk 9 September 2026 · Pacajá, Pará"


def test_build_gallery_empty_transcript_without_prefix_uses_site_and_date():
    out = g.build_gallery(
        "demo-farm",
        [_v("a", "IMG_9509.MOV", "2026-09-09T12:00:00+0000", text="")],
        site_name="Sítio Cacau na Veia",
    )
    assert out["gallery"][0]["caption"] == "Sítio Cacau na Veia — 2026-09-09"


def test_build_gallery_skips_entries_without_yt_id():
    out = g.build_gallery(
        "demo-farm", [_v(None, "IMG_1.MOV", "2026-09-09T12:00:00+0000")]
    )
    assert out["gallery"] == []


def test_build_gallery_deterministic_and_idempotent():
    vids = [
        _v("c", "IMG_9.MOV", "2026-09-09T13:00:00+0000"),
        _v("a", "IMG_2.MOV", "2026-09-09T12:00:00+0000"),
    ]
    first = g.dumps(g.build_gallery("demo-farm", vids, site_name="Demo"))
    second = g.dumps(
        g.build_gallery("demo-farm", list(reversed(vids)), site_name="Demo")
    )
    assert first == second
    assert first.endswith("\n")


# --- manifest fallback ------------------------------------------------------


def test_videos_from_manifest_reads_items_with_yt_id():
    manifest = {
        "farm_id": "demo-farm",
        "site_name": "Demo",
        "items": [
            {
                "file": "IMG_1.MOV",
                "yt_id": "a",
                "creation_date": "2026:09:09",
                "transcription": "t1",
            },
            {"file": "IMG_2.MOV", "yt_id": None, "transcription": "t2"},
            {"file": "IMG_3.MOV", "yt_id": "c", "transcription": ""},
        ],
    }
    vids = g.videos_from_manifest(manifest, probe_dir=None)
    assert [v["yt_id"] for v in vids] == ["a", "c"]
    assert vids[0]["captured_at"] == "2026:09:09"


def test_videos_from_manifest_handles_none_and_bad_shape():
    assert g.videos_from_manifest(None) == []
    assert g.videos_from_manifest({"items": "nope"}) == []


# --- write ------------------------------------------------------------------


def test_write_gallery_creates_parents_and_round_trips(tmp_path):
    out = tmp_path / "galleries" / "demo.json"
    gal = g.build_gallery(
        "demo-farm", [_v("a", "IMG_1.MOV", "2026-09-09T12:00:00+0000")]
    )
    p = g.write_gallery(gal, out)
    assert p == out and out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["gallery"][0]["videoId"] == "a"
