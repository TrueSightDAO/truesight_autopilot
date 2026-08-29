#!/usr/bin/env python3
"""Plot the TrueSight DAO cacao sourcing network map from REAL coordinates.

Two adjacent panels (Brazil | China) with a purple export arrow bridging them,
so the two countries sit side by side instead of on opposite ends of a world
map. Numbered pins 1-5; all site detail lives in the bottom legend panel
(no on-map text boxes -> zero label overlap).

Replaces the old Gemini-generated AI map and the single-panel world view whose
wide Brazil/China gap Gary flagged on 2026-08-29.

Dependencies: matplotlib, Natural Earth 50m GeoJSON (auto-downloaded once).
Output: cacao_sourcing_network_map.png

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
BR_XLIM = (-78.5, -30.0)
BR_YLIM = (-36.0, 9.0)
CN_XLIM = (103.5, 126.5)
CN_YLIM = (17.0, 33.5)
PIN_COLORS = {n: ("#c0392b" if n in BRAZIL_PINS else "#e67e22") for n in range(1, 6)}


def _load_world() -> dict:
    if not NE_CACHE.exists():
        print(f"downloading Natural Earth 50m -> {NE_CACHE}", file=sys.stderr)
        urllib.request.urlretrieve(NE_URL, NE_CACHE)
    with open(NE_CACHE, encoding="utf-8") as fh:
        return json.load(fh)


def _polygons(world: dict, names: set[str] | None = None) -> list:
    out = []
    for feat in world["features"]:
        props = feat.get("properties") or {}
        name = props.get("NAME") or props.get("ADMIN") or ""
        if names is not None and name not in names:
            continue
        g = feat["geometry"]
        if g["type"] == "Polygon":
            out.append((name, g["coordinates"]))
        elif g["type"] == "MultiPolygon":
            out.extend((name, c) for c in g["coordinates"])
    return out


def _plot_polys(ax, polys, fc, ec, lw=0.6, zorder=2) -> None:
    for _name, poly in polys:
        ring = poly[0]
        ax.fill(
            [p[0] for p in ring],
            [p[1] for p in ring],
            fc=fc,
            ec=ec,
            lw=lw,
            zorder=zorder,
        )


def _draw_pin(ax, lng, lat, num, ring=False) -> None:
    color = PIN_COLORS[num]
    ax.plot(lng, lat, "o", ms=14, mfc=color, mec="white", mew=2.0, zorder=6)
    ax.text(
        lng,
        lat,
        str(num),
        ha="center",
        va="center",
        fontsize=9,
        fontweight="bold",
        color="white",
        zorder=7,
    )
    if ring:
        ax.add_patch(
            Circle(
                (lng, lat), 1.9, fill=False, ec="#c0392b", lw=1.6, alpha=0.9, zorder=5
            )
        )


def _style_panel(ax, title) -> None:
    ax.set_title(title, fontsize=13, fontweight="bold", color="#3e5d3a", pad=8)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_facecolor("#f7fbf7")


def _draw_legend(ax) -> None:
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.text(
        0.005,
        0.97,
        "Network legend \u2014 pin = location \u00b7 constraints below \u00b7 plotted from real coordinates (OSM Nominatim)",
        fontsize=11,
        fontweight="bold",
        color="#3e5d3a",
        va="top",
    )
    n = len(SITES)
    row_h = 0.78 / n
    for i, (num, name, _lat, _lng, role, constraint) in enumerate(SITES):
        y_top = 0.94 - i * row_h
        color = PIN_COLORS[num]
        ax.text(
            0.005,
            y_top,
            str(num),
            fontsize=10,
            fontweight="bold",
            color="white",
            bbox=dict(boxstyle="circle,pad=0.30", fc=color, ec="white", lw=1.2),
            va="center",
        )
        ccol = "#c0392b" if "BLOCKER" in constraint else "#555555"
        ax.text(
            0.055,
            y_top,
            f"{name} \u2014 {role}",
            fontsize=9.5,
            fontweight="bold",
            color="#4a2a10",
            va="top",
        )
        ax.text(
            0.055,
            y_top - row_h * 0.42,
            constraint,
            fontsize=9,
            color=ccol,
            va="top",
        )
    ax.text(
        0.005,
        0.10,
        "\u26a0 = export blocker (Ilh\u00e9us: Black King CNPJ INAPTA)   \u00b7   red = Brazil network   \u00b7   orange = destination (China)   \u00b7   purple arrow = Brazil \u2192 China export lane",
        fontsize=9,
        color="#555555",
        va="top",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", nargs="?", default="/tmp/cacao_sourcing_network_map.png")
    args = ap.parse_args()

    world = _load_world()
    all_polys = _polygons(world)
    brazil = _polygons(world, {"Brazil"})
    china = _polygons(world, {"China", "Hong Kong"})
    if not brazil or not china:
        print("error: could not extract Brazil/China outlines", file=sys.stderr)
        return 1

    fig = plt.figure(figsize=(16.5, 9.2), dpi=160)
    fig.patch.set_facecolor("white")
    gs = GridSpec(
        2,
        2,
        height_ratios=[3.3, 1.6],
        width_ratios=[1.0, 1.0],
        hspace=0.22,
        wspace=0.20,
    )

    ax_br = fig.add_subplot(gs[0, 0])
    _style_panel(ax_br, "Brazil — sourcing network (pins 1–4)")
    _plot_polys(ax_br, all_polys, fc="#e9e9e9", ec="#cccccc", lw=0.4, zorder=1)
    _plot_polys(ax_br, brazil, fc="#cdeacb", ec="#3e7d4e", lw=1.2, zorder=2)
    ax_br.set_xlim(*BR_XLIM)
    ax_br.set_ylim(*BR_YLIM)

    ax_cn = fig.add_subplot(gs[0, 1])
    _style_panel(ax_cn, "China — destination (pin 5)")
    _plot_polys(ax_cn, all_polys, fc="#e9e9e9", ec="#cccccc", lw=0.4, zorder=1)
    _plot_polys(ax_cn, china, fc="#fde8d7", ec="#c97a3d", lw=1.2, zorder=2)
    ax_cn.set_xlim(*CN_XLIM)
    ax_cn.set_ylim(*CN_YLIM)

    for num, _name, lat, lng, _role, constraint in SITES:
        _draw_pin(
            ax_br if num in BRAZIL_PINS else ax_cn,
            lng,
            lat,
            num,
            ring="BLOCKER" in constraint,
        )

    # export arrow bridging the two panels (figure fraction coords)
    fig.add_artist(
        FancyArrowPatch(
            posA=(0.462, 0.60),
            posB=(0.538, 0.60),
            transform=fig.transFigure,
            connectionstyle="arc3,rad=0.12",
            arrowstyle="-|>",
            mutation_scale=32,
            lw=3.2,
            color="#8e44ad",
            zorder=20,
        )
    )
    fig.text(
        0.5,
        0.705,
        "Export lane · Brazil → China",
        ha="center",
        fontsize=11.5,
        fontweight="bold",
        color="#8e44ad",
    )

    fig.suptitle(
        "TrueSight DAO — Cacao Sourcing Network (Brazil → China)",
        fontsize=17,
        fontweight="bold",
        color="#3e5d3a",
        y=0.995,
    )
    fig.text(
        0.5,
        0.952,
        "Plotted from real coordinates — not AI-generated",
        fontsize=9.5,
        color="#666666",
        ha="center",
    )

    leg = fig.add_subplot(gs[1, :])
    _draw_legend(leg)

    fig.savefig(
        args.output, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor()
    )
    print(f"saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
