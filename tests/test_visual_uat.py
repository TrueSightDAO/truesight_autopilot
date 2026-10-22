"""Unit tests for the pure helpers behind ``visual_uat``.

The browser and Gemini paths are proven live in the PR description; these
tests pin the decision logic that must never regress:

- a model's prose can NEVER turn a pass into a fail
- collapsed elements (content present, zero height) ARE caught, which is the
  ``onerror -> display:none`` family a ``naturalWidth === 0`` check misses
- retired model names are never selected
"""

from __future__ import annotations

from app.tools.visual_uat import (
    build_verdict,
    evaluate_assertions,
    find_collapsed,
    parse_findings,
    pick_vision_model,
    rank_vision_models,
)


class TestEvaluateAssertions:
    def test_exact_match_passes(self):
        rows = evaluate_assertions(
            {"brazilian-farms-count": "28"}, {"brazilian-farms-count": "28"}
        )
        assert len(rows) == 1
        assert rows[0]["pass"] is True
        assert rows[0]["actual"] == "28"

    def test_mismatch_fails_and_keeps_actual(self):
        rows = evaluate_assertions(
            {"brazilian-farms-count": "22"}, {"brazilian-farms-count": "28"}
        )
        assert rows[0]["pass"] is False
        assert rows[0]["actual"] == "22", (
            "the actual value must survive for diagnostics"
        )

    def test_missing_element_fails(self):
        rows = evaluate_assertions({"nope": None}, {"nope": "28"})
        assert rows[0]["pass"] is False
        assert rows[0]["actual"] is None

    def test_whitespace_and_dash_normalised(self):
        rows = evaluate_assertions({"d": "  ~1,597 "}, {"d": "~1,597"})
        assert rows[0]["pass"] is True

    def test_em_dash_equivalent_to_hyphen(self):
        # &mdash; renders as an em dash; must not fail against a plain hyphen
        rows = evaluate_assertions({"d": "\u2014"}, {"d": "-"})
        assert rows[0]["pass"] is True

    def test_no_expectations_yields_no_rows(self):
        assert evaluate_assertions({"a": "1"}, None) == []


class TestFindCollapsed:
    def test_content_with_zero_height_is_collapsed(self):
        collapsed = find_collapsed(
            [{"selector": "img.hero", "content_len": 1, "height": 0}]
        )
        assert len(collapsed) == 1
        assert collapsed[0]["selector"] == "img.hero"

    def test_near_zero_height_is_collapsed(self):
        assert len(find_collapsed([{"content_len": 5, "height": 1}])) == 1

    def test_visible_element_is_not_flagged(self):
        assert find_collapsed([{"content_len": 5, "height": 40}]) == []

    def test_merged_nested_text_is_not_flagged(self):
        # A wrapper whose children carry the text has height; must not flag.
        assert find_collapsed([{"content_len": 900, "height": 320}]) == []

    def test_empty_element_is_not_flagged(self):
        assert find_collapsed([{"content_len": 0, "height": 0}]) == []


class TestBuildVerdict:
    def test_all_green_passes(self):
        assert build_verdict([{"pass": True}], [], []) == "pass"

    def test_failed_assertion_fails(self):
        assert build_verdict([{"pass": False}], [], []) == "fail"

    def test_console_error_fails(self):
        assert build_verdict([{"pass": True}], ["boom"], []) == "fail"

    def test_failed_request_fails(self):
        assert build_verdict([], [], ["missing.png"]) == "fail"

    def test_model_prose_cannot_affect_verdict(self):
        # Signature has no findings parameter at all — the invariant is structural.
        assert build_verdict([{"pass": True}], [], []) == "pass"


class TestPickVisionModel:
    def _payload(self, *names):
        return {
            "models": [
                {
                    "name": f"models/{n}",
                    "supportedGenerationMethods": ["generateContent"],
                }
                for n in names
            ]
        }

    def test_prefers_highest_version_flash(self):
        got = pick_vision_model(self._payload("gemini-2.5-flash", "gemini-3.8-flash"))
        assert got == "gemini-3.8-flash"

    def test_skips_image_generation_models(self):
        got = pick_vision_model(
            self._payload("gemini-2.5-flash-image", "gemini-3.8-flash")
        )
        assert got == "gemini-3.8-flash"

    def test_skips_models_without_generate_content(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-3.8-flash",
                    "supportedGenerationMethods": ["embedContent"],
                },
                {
                    "name": "models/gemini-2.5-pro",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
        assert pick_vision_model(payload) == "gemini-2.5-pro"

    def test_returns_none_when_nothing_usable(self):
        assert pick_vision_model({"models": []}) is None
        assert pick_vision_model({}) is None

    def test_prefers_flash_on_equal_version(self):
        got = pick_vision_model(self._payload("gemini-3.5-pro", "gemini-3.5-flash"))
        assert got == "gemini-3.5-flash"


class TestRankVisionModels:
    def test_returns_ordered_shortlist_not_one(self):
        # One name is not enough: advertised capabilities cannot be trusted, so
        # the caller needs fallbacks to probe.
        got = rank_vision_models(
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.8-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert len(got) >= 2
        assert got[0] == "gemini-3.8-flash"

    def test_excludes_interactions_api_only_models(self):
        # gemini-omni-* advertises generateContent but rejects at call time.
        got = rank_vision_models(
            {
                "models": [
                    {
                        "name": "models/gemini-omni-flash-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.8-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert "gemini-omni-flash-preview" not in got
        assert "gemini-3.8-flash" in got

    def test_excludes_tts_and_image_models(self):
        got = rank_vision_models(
            {
                "models": [
                    {
                        "name": "models/gemini-3.8-flash-tts",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/nano-banana-pro-preview",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.8-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert got == ["gemini-3.8-flash"]

    def test_lite_ranks_below_full(self):
        got = rank_vision_models(
            {
                "models": [
                    {
                        "name": "models/gemini-3.5-flash-lite",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-3.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }
        )
        assert got[0] == "gemini-3.5-flash"

    def test_limit_respected(self):
        models = [
            {
                "name": f"models/gemini-3.{i}-flash",
                "supportedGenerationMethods": ["generateContent"],
            }
            for i in range(9)
        ]
        assert len(rank_vision_models({"models": models}, limit=3)) == 3


class TestParseFindings:
    def test_findings_are_always_unverified(self):
        text = "1. **Cart button overflows** the header on mobile viewports badly."
        findings = parse_findings(text)
        assert findings
        assert all(f["verified"] is False for f in findings)

    def test_preamble_lines_dropped(self):
        text = "Based on the screenshots, here are the defects:\nHero text is hard to read over bright pods."
        findings = parse_findings(text)
        assert len(findings) == 1
        assert "hard to read" in findings[0]["finding"]

    def test_clean_page_claim_yields_no_findings(self):
        assert parse_findings("Page looks clean") == []

    def test_markdown_stripped(self):
        findings = parse_findings(
            "1. **Bold** and `code` inside a long enough sentence here."
        )
        assert findings
        assert "**" not in findings[0]["finding"]

    def test_caps_respected(self):
        text = "\n".join(
            f"Defect number {i} is a genuinely long enough description."
            for i in range(20)
        )
        assert len(parse_findings(text, max_findings=3)) == 3


# ── viewport-mode coverage + verdict scoping (regression) ─────────────────────


def test_required_modes_both_present():
    from app.tools.visual_uat import missing_required_modes

    vps = [{"label": "desktop"}, {"label": "mobile"}]
    assert missing_required_modes(vps) == []


def test_required_modes_flags_missing_mobile():
    from app.tools.visual_uat import missing_required_modes

    assert missing_required_modes([{"label": "desktop"}]) == ["mobile"]


def test_analytics_beacon_does_not_fail_verdict():
    """GA4 collect aborts from headless Chromium; must not fail a clean page."""
    from app.tools.visual_uat import build_verdict, classify_request_failures

    failed = [
        {
            "viewport": "desktop",
            "url": "https://www.google-analytics.com/g/collect?v=2&tid=G-S6EP25EHF4",
        }
    ]
    page, third, analytics = classify_request_failures(
        failed, "https://agroverse.shop/"
    )
    assert page == []
    assert len(analytics) == 1
    assert third == []
    assert build_verdict([{"pass": True}], [], page) == "pass"


def test_same_origin_missing_asset_fails_verdict():
    from app.tools.visual_uat import build_verdict, classify_request_failures

    failed = [
        {"viewport": "mobile", "url": "https://agroverse.shop/js/journey-stats.js"}
    ]
    page, _, analytics = classify_request_failures(failed, "https://agroverse.shop/")
    assert len(page) == 1
    assert analytics == []
    assert build_verdict([{"pass": True}], [], page) == "fail"


def test_thirdparty_non_analytics_is_reported_not_failed():
    from app.tools.visual_uat import build_verdict, classify_request_failures

    failed = [
        {"viewport": "desktop", "url": "https://fonts.gstatic.com/s/x.woff2"},
    ]
    page, third, analytics = classify_request_failures(
        failed, "https://agroverse.shop/"
    )
    assert page == []
    assert len(third) == 1
    assert analytics == []
    assert build_verdict([{"pass": True}], [], page) == "pass"


def test_page_asset_helper():
    from app.tools.visual_uat import is_page_asset

    assert is_page_asset("https://x.shop/js/a.js?v=2") is True
    assert is_page_asset("https://x.shop/img/a.webp") is True
    assert is_page_asset("https://x.shop/api/track") is False
