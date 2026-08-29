#!/usr/bin/env python3
"""Plot the TrueSight DAO cacao sourcing network map from REAL coordinates.

Replaces the old Gemini-generated AI map (pins were decorative, not geocoded).

Dependencies: matplotlib, a Natural Earth 50m GeoJSON (auto-downloaded once
if missing). Output: cacao_sourcing_network_map.png.

Usage: python3 plot_cacao_sourcing_network_map.py [output.png]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch  # noqa: E402

NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_CACHE = Path("/tmp/ne50_countries.geojson")

# name -> (lat, lng, role) — geocoded via OSM Nominatim, 2026-08-29
SITES = [
    ("Manicoré (AM)", -5.8046180, -61.2894830, "exploratory source"),
    ("Altamira (PA)", -3.2040650, -52.2099610, "beans supplier (CEPOTX)"),
    ("Itabuna (BA)", -14.7931730, -39.2750341, "conversion + export (Coopercabruca)"),
    ("Ilhéus (BA)", -14.7925990, -39.0453843, "exporter / warehouse (Black King)"),
    ("Dongguan (CN)", 23.0183568, 113.7452332, "destination market (Elizabeth Wong)"),
]


def _load_world() -> dict:
    if not NE_CACHE.exists():
        print(f"downloading Natural Earth 50m -> {NE_CACHE}", file=sys.stderr)
        urllib.request.urlretrieve(NE_URL, NE_CACHE)
    with open(NE_CACHE, encoding="utf-8") as fh:
        return json.load(fh)


def _polys(world: dict, names: set[str]) -> list:
    out: list = []
    for feat in world["features"]:
        props = feat.get("properties") or {}
        name = props.get("NAME") or props.get("ADMIN") or ""
        if name not in names:
            continue
        g = feat["geometry"]
        if g["type"] == "Polygon":
            out.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            out.extend(g["coordinates"])
    return out


def _plot_polys(ax, polys: list, **kw) -> None:
    for poly in polys:
        ring = poly[0]
        ax.fill([p[0] for p in ring], [p[1] for p in ring], **kw)
        for hole in poly[1:]:
            ax.fill([p[0] for p in hole], [p[1] for p in hole], color="white", zorder=2.1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", nargs="?", default="/tmp/cacao_sourcing_network_map.png")
    args = ap.parse_args()

    world = _load_world()
    brazil = _polys(world, {"Brazil"})
    china = _polys(world, {"China", "Hong Kong"})
    if not brazil or not china:
        print("error: could not extract Brazil/China outlines from GeoJSON", file=sys.stderr)
        return 1

    fig, ax = plt.subplots(figsize=(14, 8), dpi=160)
    ax.set_facecolor("#f4f9f4")
    _plot_polys(ax, brazil, color="#d5ecd4", edgecolor="#3e7d4e", linewidth=1.2, zorder=2)
    _plot_polys(ax, china, color="#fde8d7", edgecolor="#c97a3d", linewidth=1.2, zorder=2)

    for name, lat, lng, role in SITES:
        if lng < -20:  # Brazil
            ax.plot(lng, lat, "o", ms=11, mfc="#c0392b", mec="white", mew=1.6, zorder=6)
            ax.annotate(
                name,
                xy=(lng, lat),
                xytext=(lng + 0.4, lat + 0.6),
                fontsize=9.5,
                fontweight="bold",
                color="#5a2d0c",
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#c0392b", alpha=0.92, lw=0.8),
            )
        else:  # China
            ax.plot(lng, lat, "o", ms=11, mfc="#e67e22", mec="white", mew=1.6, zorder=6)
            ax.annotate(
                name,
                xy=(lng, lat),
                xytext=(lng - 8.5, lat - 2.2),
                fontsize=9.5,
                fontweight="bold",
                color="#5a2d0c",
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#e67e22", alpha=0.92, lw=0.8),
            )

    # sourcing flow arrow Brazil -> China
    br_ring = brazil[0][0]
    cn_ring = china[0][0]
    ax.add_patch(
        FancyArrowPatch(
            posA=(
                max(p[0] for p in br_ring) - 2,
                (min(p[1] for p in br_ring) + max(p[1] for p in br_ring)) / 2,
            ),
            posB=(min(p[0] for p in cn_ring) + 2, 23.5),
            connectionstyle="arc3,rad=0.12",
            arrowstyle="-|>",
            mutation_scale=26,
            lw=2.2,
            color="#8e44ad",
            alpha=0.85,
            zorder=5,
        )
    )
    ax.text(
        (max(p[0] for p in br_ring) + min(p[0] for p in cn_ring)) / 2,
        31,
        "Export lane  Brazil \u2192 China",
        fontsize=10.5,
        color="#8e44ad",
        fontweight="bold",
        ha="center",
        zorder=7,
    )

    ax.set_xlim(-74, 122)
    ax.set_ylim(-34, 50)
    ax.set_aspect(1.15)
    ax.set_title(
        "TrueSight DAO — Cacao Sourcing Network (Brazil \u2192 China)",
        fontsize=16,
        fontweight="bold",
        color="#3e5d3a",
        pad=18,
    )
    ax.text(
        24,
        47,
        "Plotted from real coordinates (OSM Nominatim) — not AI-generated",
        fontsize=9.5,
        color="#666666",
        ha="center",
    )
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    fig.tight_layout()
    fig.savefig(args.output, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
