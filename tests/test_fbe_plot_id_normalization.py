"""Regression: FARM BOUNDARY EVIDENCE 'Plot ID' must survive normalization.

2026-09-10 (thread 24321): the Edgar catalog advertised a "- Plot ID:" line in the
FBE description but omitted "Plot ID" from canonical_labels. The legacy normalizer
then dropped a governor-supplied Plot ID, the GAS handler fell back to farm-slug
matching, and it overwrote the wrong plot row (Fazenda Cleide, B-06-108:
plot_type maturing -> research). These tests pin the three guards:

  1. _merge_catalog_labels never drops a DAO-guaranteed label.
  2. _normalize_submission_labels keeps "Plot ID" for FBE.
  3. truly non-canonical descriptive keys (e.g. "notes") are still dropped.
"""

from app.main import (
    _CANONICAL_LABELS,
    _merge_catalog_labels,
    _normalize_submission_labels,
)


def test_merge_catalog_labels_unions_plot_id():
    merged = _merge_catalog_labels(
        "FARM BOUNDARY EVIDENCE EVENT",
        ["Farm Name", "Plot Type", "Boundary Type"],
    )
    assert "Plot ID" in merged
    # also covers the realistic catalog shape (the 11 labels Edgar emits today)
    assert "Farm Name" in merged


def test_merge_catalog_labels_is_idempotent():
    once = _merge_catalog_labels("FARM BOUNDARY EVIDENCE EVENT", ["Farm Name"])
    twice = _merge_catalog_labels("FARM BOUNDARY EVIDENCE EVENT", once)
    assert twice.count("Plot ID") == 1


def test_merge_catalog_labels_noop_for_unrelated_event():
    merged = _merge_catalog_labels("SALES EVENT", ["Item", "Sales price"])
    assert merged == ["Item", "Sales price"]


def test_normalize_keeps_plot_id_for_fbe(monkeypatch):
    # Simulate the post-merge catalog state an Edgar sync produces.
    monkeypatch.setitem(
        _CANONICAL_LABELS,
        "FARM BOUNDARY EVIDENCE EVENT",
        _merge_catalog_labels(
            "FARM BOUNDARY EVIDENCE EVENT",
            ["Farm Name", "Plot Type", "Boundary Type"],
        ),
    )
    out = _normalize_submission_labels(
        "FARM BOUNDARY EVIDENCE EVENT",
        {
            "Farm Name": "Fazenda Cleide",
            "Plot ID": "B-06-108_20260908_research_1",
            "Plot Type": "research",
            "Boundary Type": "approx",
            "notes": "descriptive noise -- must be dropped",
        },
    )
    assert out["Plot ID"] == "B-06-108_20260908_research_1"
    assert out["Farm Name"] == "Fazenda Cleide"
    assert out["Plot Type"] == "research"
    assert "notes" not in out
