#!/usr/bin/env python3
"""Plot the TrueSight DAO cacao sourcing network map from REAL coordinates,
with per-site role + constraint annotations.

Replaces the old Gemini-generated AI map (pins were decorative, not geocoded)
and annotates each location's constraint (per CACAO_SOURCING_NETWORK_OVERVIEW.md).

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
from matplotlib.patches import Circle, FancyArrowPatch  # noqa: E402

NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"
NE_CACHE = Path("/tmp/ne50_countries.geojson")

# name, lat, lng, role, constraint, label offset (dx, dy in degrees)
# Geocoded via OSM Nominatim 2026-08-29; constraints per CACAO_SOURCING_NETWORK_OVERVIEW.md
SITES = [
    ("Manicoré (AM)", -5.804618, -61.289483, "exploratory source",
     "no logistics / fermentation / freight infra known", 0.9, 1.6),
    ("Altamira (PA)", -3.204065, -52.209961, "beans supplier (CEPOTX)",
     "beans only · NO conversion · needs CN-side warehouse", 0.9, 1.6),
    ("Itabuna (BA)", -14.793173, -39.275034, "conversion + export (Coopercabruca)",
     "members-only · NO warehousing · exact spec upfront", -2.2, 2.2),
    ("Ilhéus (BA)", -14.792599, -39.045384, "exporter / warehouse (Black King)",
     "⚠ CNPJ INAPTA · NO export NF-e — BLOCKER", 0.9, -2.6),
    ("Dongguan (CN)", 23.018357, 113.745233, "destination market (Elizabeth Wong)",
     "SKU spec pending", -10.5, -3.2),
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


def _annotate(ax, site, pin_color, is_blocker=False) -> None:
    name, lat, lng, role, constraint, dx, dy = site
    label = "\n".join([name, role, constraint])
    ax.plot(lng, lat, "o", ms=11, mfc=pin_color, mec="white", mew=1.6, zorder=6)
    if is_blocker:
        ring = Circle((lng, lat), 2.1, fill=False, ec="#c0392b", lw=1.4, alpha=0.9, zorder=5)
        ax.add_patch(ring)
    ax.annotate(
        label,
        xy=(lng, lat),
        xytext=(lng + dx, lat + dy),
        fontsize=8.2,
        color="#4a2a10",
        zorder=7,
        ha="left",
        va="bottom",
        linespacing=1.35,
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=pin_color, alpha=0.93, lw=1.1),
        arrowprops=dict(arrowstyle="-", color="#888888", lw=0.8, alpha=0.7),
    )


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

    fig, ax = plt.subplots(figsize=(14, 8.2), dpi=160)
    ax.set_facecolor("#f4f9f4")
    _plot_polys(ax, brazil, color="#d5ecd4", edgecolor="#3e7d4e", linewidth=1.2, zorder=2)
    _plot_polys(ax, china, color="#fde8d7", edgecolor="#c97a3d", linewidth=1.2, zorder=2)

    for site in SITES:
        name, lat, lng, role, constraint, dx, dy = site
        if lng < -20:  # Brazil
            pin_color = "#c0392b"
        else:  # China
            pin_color = "#e67e22"
        is_blocker = "BLOCKER" in constraint
        _annotate(ax, site, pin_color, is_blocker=is_blocker)

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
        24, 47,
        "Plotted from real coordinates (OSM Nominatim) — not AI-generated · constraints per 29 Aug 2026 network doc",
        fontsize=9.5,
        color="#666666",
        ha="center",
    )
    ax.text(
        24, -32.5,
        "⚠ = export blocker (Ilhéus: Black King CNPJ INAPTA) · red = Brazil network · orange = destination (China)",
        fontsize=9,
        color="#555555",
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
