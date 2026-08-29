#!/usr/bin/env python3
"""Plot the TrueSight DAO cacao sourcing network map from REAL coordinates,
numbered pins + legend panel (no on-map text boxes -> zero label overlap).

Replaces the old Gemini-generated AI map and the earlier on-map annotation
layout (whose Bahia labels overlapped). Pin numbers 1-5 reference the legend.

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
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.patches import Circle, FancyArrowPatch  # noqa: E402

NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_CACHE = Path("/tmp/ne50_countries.geojson")

# number, name, lat, lng, role, constraint
# Geocoded via OSM Nominatim 2026-08-29; constraints per CACAO_SOURCING_NETWORK_OVERVIEW.md
SITES = [
    (
        1,
        "Manicoré (AM)",
        -5.804618,
        -61.289483,
        "exploratory source",
        "no logistics / fermentation / freight infra known",
    ),
    (
        2,
        "Altamira (PA)",
        -3.204065,
        -52.209961,
        "beans supplier (CEPOTX)",
        "beans only · NO conversion · needs CN-side warehouse",
    ),
    (
        3,
        "Itabuna (BA)",
        -14.793173,
        -39.275034,
        "conversion + export (Coopercabruca)",
        "members-only · NO warehousing · exact spec upfront",
    ),
    (
        4,
        "Ilhéus (BA)",
        -14.792599,
        -39.045384,
        "exporter / warehouse (Black King)",
        "⚠ CNPJ INAPTA · NO export NF-e — BLOCKER",
    ),
    (
        5,
        "Dongguan (CN)",
        23.018357,
        113.745233,
        "destination market (Elizabeth Wong)",
        "SKU spec pending",
    ),
]
BRAZIL_PINS = {1, 2, 3, 4}


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
            ax.fill(
                [p[0] for p in hole], [p[1] for p in hole], color="white", zorder=2.1
            )


def _draw_pin(ax, lng, lat, num, pin_color) -> None:
    ax.plot(lng, lat, "o", ms=13, mfc=pin_color, mec="white", mew=1.8, zorder=6)
    ax.text(
        lng,
        lat,
        str(num),
        ha="center",
        va="center",
        fontsize=8,
        fontweight="bold",
        color="white",
        zorder=7,
    )


def _draw_legend(ax, world) -> None:
    """Right-hand legend panel: number -> site -> role -> constraint."""
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(
        0.02,
        0.97,
        "Network legend",
        fontsize=13,
        fontweight="bold",
        color="#3e5d3a",
        va="top",
    )
    ax.text(
        0.02,
        0.935,
        "(plotted from real coordinates — OSM Nominatim)",
        fontsize=8,
        color="#888888",
        va="top",
    )
    y = 0.87
    for num, name, lat, lng, role, constraint in SITES:
        color = "#c0392b" if num in BRAZIL_PINS else "#e67e22"
        ax.text(
            0.02,
            y,
            f"{num}",
            fontsize=10,
            fontweight="bold",
            color="white",
            bbox=dict(boxstyle="circle,pad=0.28", fc=color, ec="white", lw=1.0),
            va="top",
        )
        ax.text(
            0.08,
            y,
            f"{name} — {role}",
            fontsize=9.5,
            fontweight="bold",
            color="#4a2a10",
            va="top",
        )
        ax.text(0.08, y - 0.032, constraint, fontsize=8.4, color="#555555", va="top")
        if "BLOCKER" in constraint:
            ax.text(
                0.08,
                y - 0.032,
                constraint,
                fontsize=8.4,
                color="#c0392b",
                va="top",
                fontweight="bold",
            )
        y -= 0.145
    ax.text(
        0.02,
        y - 0.015,
        "⚠ = export blocker (Ilhéus: Black King CNPJ INAPTA)",
        fontsize=8,
        color="#c0392b",
        va="top",
    )
    ax.text(
        0.02,
        y - 0.05,
        "red = Brazil network · orange = destination (China)",
        fontsize=8,
        color="#555555",
        va="top",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", nargs="?", default="/tmp/cacao_sourcing_network_map.png")
    args = ap.parse_args()

    world = _load_world()
    brazil = _polys(world, {"Brazil"})
    china = _polys(world, {"China", "Hong Kong"})
    if not brazil or not china:
        print(
            "error: could not extract Brazil/China outlines from GeoJSON",
            file=sys.stderr,
        )
        return 1

    fig = plt.figure(figsize=(16.5, 8.2), dpi=160)
    fig.patch.set_facecolor("white")
    gs = GridSpec(1, 2, width_ratios=[2.55, 1.0], wspace=0.05)
    ax = fig.add_subplot(gs[0])
    ax.set_facecolor("#f4f9f4")
    _plot_polys(
        ax, brazil, color="#d5ecd4", edgecolor="#3e7d4e", linewidth=1.2, zorder=2
    )
    _plot_polys(
        ax, china, color="#fde8d7", edgecolor="#c97a3d", linewidth=1.2, zorder=2
    )

    for num, name, lat, lng, role, constraint in SITES:
        pin_color = "#c0392b" if num in BRAZIL_PINS else "#e67e22"
        _draw_pin(ax, lng, lat, num, pin_color)
        if "BLOCKER" in constraint:
            ax.add_patch(
                Circle(
                    (lng, lat),
                    2.1,
                    fill=False,
                    ec="#c0392b",
                    lw=1.4,
                    alpha=0.9,
                    zorder=5,
                )
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
        "TrueSight DAO \u2014 Cacao Sourcing Network (Brazil \u2192 China)",
        fontsize=16,
        fontweight="bold",
        color="#3e5d3a",
        pad=18,
    )
    ax.text(
        24,
        47,
        "Plotted from real coordinates \u2014 not AI-generated \u00b7 constraints per 29 Aug 2026 network doc",
        fontsize=9.5,
        color="#666666",
        ha="center",
    )
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    leg = fig.add_subplot(gs[1])
    _draw_legend(leg, world)

    fig.savefig(
        args.output, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor()
    )
    print(f"saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
