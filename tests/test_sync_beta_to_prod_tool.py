"""Unit tests for the sync_beta_to_prod tool — blessed generated-file
auto-resolve on a merge conflict (409), and the never-force invariant.

Regression guard for the fix to the recurring promotion blocker: both a beta
repo and its prod fork run the same stats generators on a schedule, so their
independent "chore(stats): refresh" bot commits diverge ``stats/*.json`` and
block ``merge-upstream`` with a 409 even when the reviewed change merges
cleanly. The tool now takes BETA's copy of the blessed generated paths
(``settings.prod_sync_generated_globs``) and retries ONCE. Nothing outside the
blessed globs is ever touched.
"""

from __future__ import annotations

import httpx

from app.tools import sync_beta_to_prod as mod


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _patch_lease(monkeypatch):
    monkeypatch.setattr(mod, "check_lease", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(mod, "acquire_lease", lambda *a, **k: {"status": "error"})
    monkeypatch.setattr(mod, "close_lease", lambda *a, **k: None)
    monkeypatch.setattr(mod, "append_deploy_record", lambda **k: {"status": "success"})
    monkeypatch.setattr(mod.settings, "github_pat", "test-pat")


def test_unknown_repo_rejected():
    out = mod.sync_beta_to_prod("not_a_prod_repo")
    assert out["status"] == "error"
    assert "not a known production repo" in out["message"]


def test_conflict_with_blessed_generated_files_auto_resolves_then_succeeds(
    monkeypatch,
):
    """409 -> take beta's copy of the blessed stats files -> retry succeeds."""
    _patch_lease(monkeypatch)
    monkeypatch.setattr(mod.settings, "prod_sync_generated_globs", ["stats/*.json"])

    posts = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        posts["n"] += 1
        if posts["n"] == 1:
            return _Resp(409, {"message": "There are merge conflicts"})
        return _Resp(200, {"merge_type": "fast-forward", "message": "Synced."})

    def fake_get(url, headers=None, timeout=None):
        # Single-file fetch (match this BEFORE the dir-listing prefixes, since
        # the file URL also contains the "/contents/stats" substring).
        if url.endswith("/contents/stats/current.json?ref=main"):
            return _Resp(200, {"content": "eyJhIjoxfQ=="})
        if "/truesight_me_prod/contents/stats" in url:
            return _Resp(
                200,
                [
                    {"path": "stats/current.json", "sha": "prod-1", "type": "file"},
                    {"path": "stats/sunmint_index.json", "sha": "same", "type": "file"},
                ],
            )
        if "/truesight_me_beta/contents/stats" in url:
            return _Resp(
                200,
                [
                    {"path": "stats/current.json", "sha": "beta-1", "type": "file"},
                    {"path": "stats/sunmint_index.json", "sha": "same", "type": "file"},
                ],
            )
        raise AssertionError(f"unexpected GET {url}")

    puts = []

    def fake_put(url, headers=None, json=None, timeout=None):
        puts.append(url)
        return _Resp(200, {"commit": {"sha": "abc"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", fake_put)

    out = mod.sync_beta_to_prod("truesight_me_prod")

    assert out["status"] == "ok"
    assert out["auto_resolved"] == ["stats/current.json"]
    assert posts["n"] == 2  # synced once more after the auto-resolve
    # Only the diverged blessed file was written; identical files were skipped.
    assert puts and puts[0].endswith("/truesight_me_prod/contents/stats/current.json")


def test_conflict_outside_blessed_globs_never_writes_and_reports(monkeypatch):
    """A 409 with no blessed-file divergence must NOT write anything."""
    _patch_lease(monkeypatch)
    monkeypatch.setattr(mod.settings, "prod_sync_generated_globs", ["stats/*.json"])

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(409, {"message": "There are merge conflicts"})

    def fake_get(url, headers=None, timeout=None):
        # stats/ identical across forks; the real conflict is elsewhere.
        return _Resp(
            200, [{"path": "stats/current.json", "sha": "same", "type": "file"}]
        )

    puts = []
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", lambda *a, **k: puts.append(a) or _Resp(200))

    out = mod.sync_beta_to_prod("truesight_me_prod")

    assert out["status"] == "conflict"
    assert out["auto_resolved"] == []
    assert puts == []  # never force, never touch an unblessed path


def test_empty_glob_list_disables_auto_resolve(monkeypatch):
    """Globs are the safety switch: empty => plain conflict report, no writes."""
    _patch_lease(monkeypatch)
    monkeypatch.setattr(mod.settings, "prod_sync_generated_globs", [])

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Resp(409, {"message": "conflict"})
    )
    puts = []
    monkeypatch.setattr(httpx, "put", lambda *a, **k: puts.append(a) or _Resp(200))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, []))

    out = mod.sync_beta_to_prod("truesight_me_prod")
    assert out["status"] == "conflict"
    assert puts == []


def test_runaway_divergence_is_not_mass_written(monkeypatch):
    """More than _MAX_AUTO_RESOLVE diverged files => refuse to mass-overwrite."""
    _patch_lease(monkeypatch)
    monkeypatch.setattr(mod.settings, "prod_sync_generated_globs", ["stats/*.json"])

    many_prod = [
        {"path": f"stats/f{i}.json", "sha": "p", "type": "file"} for i in range(60)
    ]
    many_beta = [
        {"path": f"stats/f{i}.json", "sha": "b", "type": "file"} for i in range(60)
    ]

    def fake_get(url, headers=None, timeout=None):
        if "beta" in url and "/contents/stats" in url:
            return _Resp(200, many_beta)
        if "/contents/stats" in url:
            return _Resp(200, many_prod)
        return _Resp(200, {"content": "eA=="})

    puts = []
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Resp(409, {"message": "conflict"})
    )
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "put", lambda *a, **k: puts.append(a) or _Resp(200))

    out = mod.sync_beta_to_prod("truesight_me_prod")
    assert out["status"] == "conflict"
    assert puts == []  # guard tripped: refused the mass write


def test_success_path_returns_ok(monkeypatch):
    _patch_lease(monkeypatch)

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _Resp(200, {"merge_type": "merge", "message": "Synced."}),
    )
    out = mod.sync_beta_to_prod("truesight_me_prod")
    assert out["status"] == "ok"
    assert out["prod_repo"] == "truesight_me_prod"
