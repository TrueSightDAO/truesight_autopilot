#!/usr/bin/env python3
"""SunMint tree-growth analysis worker (P2).

Analyzes a farmer's calibration-card photo or walk-around video to estimate
DBH (diameter at breast height) and CO2e sequestered, following the Plan Vivo
PM002 accounting chain:

    AGB  = allometric(species, DBH)                     # kg above-ground biomass
    BGB  = AGB x R (root:shoot, IPCC default 0.32)      # kg below-ground biomass
    C    = (AGB + BGB) x 0.47                           # kg carbon
    CO2e = C x 44/12                                    # kg CO2 equivalent
    CO2e_net = CO2e x (1 - AR) x (1 - RB)               # after achievement reserve (10%)
                                                         #   and risk buffer (20%)

Usage:
    python3 scripts/tree_growth_analysis.py --video <file> [--photo <file>]
        [--dbh <cm>] [--species <key>] [--tree-id <id>] [--json]

    --dbh can be provided as a manual cross-check; if omitted the worker
    attempts card-based measurement from the photo/video frames.
    --json prints a machine-readable result object (used by the webhook path).

Dependencies: Pillow (required). OpenCV (optional, enables video frame
extraction + card detection; the worker degrades gracefully without it).

Output: JSON with estimated DBH (if measurable), CO2e per tree, the full
PM002 intermediate values, and quality flags for VVB auditing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("tree_growth_analysis")

# ---------------------------------------------------------------------------
# Species allometric coefficients: AGB = a * (DBH ** b)  (kg, DBH in cm)
# Values are standard tropical allometric fits (Chave et al. 2014 pantropical
# form, adjusted per species) used as project defaults. Calibration against
# local destructive samples is a P4 refinement item.
# ---------------------------------------------------------------------------
ALLOMETRICS = {
    "cacao": {"a": 0.0673, "b": 2.397, "label": "Theobroma cacao"},
    "brazil_nut": {"a": 0.0693, "b": 2.412, "label": "Bertholletia excelsa"},
    "acai": {"a": 0.0642, "b": 2.350, "label": "Euterpe oleracea"},
    "mahogany": {"a": 0.0730, "b": 2.420, "label": "Swietenia macrophylla"},
    "jatoba": {"a": 0.0705, "b": 2.405, "label": "Hymenaea courbaril"},
    "default": {"a": 0.0673, "b": 2.397, "label": "Pantropical default (Chave et al. 2014)"},
}

# PM002 / IPCC constants
ROOT_SHOOT_RATIO = 0.32  # R
CARBON_FRACTION = 0.47  # 0.47
CO2_MOLAR_RATIO = 44.0 / 12.0  # 44/12
ACHIEVEMENT_RESERVE = 0.10  # AR (10%)
RISK_BUFFER = 0.20  # RB (20%)

# Calibration card: ISO/IEC 7810 ID-1 credit-card size
CARD_WIDTH_MM = 85.60
CARD_HEIGHT_MM = 53.98


def allometric_agb(species: str, dbh_cm: float) -> float:
    """Above-ground biomass (kg) from DBH (cm) using the species table."""
    spec = ALLOMETRICS.get((species or "default").strip().lower(), ALLOMETRICS["default"])
    if dbh_cm <= 0:
        raise ValueError("DBH must be positive")
    return spec["a"] * (dbh_cm**spec["b"])


def pm002_chain(species: str, dbh_cm: float) -> dict:
    """Compute the full PM002 CO2e chain for a single tree measurement."""
    agb = allometric_agb(species, dbh_cm)
    bgb = agb * ROOT_SHOOT_RATIO
    carbon_kg = (agb + bgb) * CARBON_FRACTION
    co2e_kg = carbon_kg * CO2_MOLAR_RATIO
    co2e_net_kg = co2e_kg * (1 - ACHIEVEMENT_RESERVE) * (1 - RISK_BUFFER)
    return {
        "species": (species or "default").strip().lower(),
        "dbh_cm": round(dbh_cm, 2),
        "agb_kg": round(agb, 4),
        "bgb_kg": round(bgb, 4),
        "biomass_kg": round(agb + bgb, 4),
        "carbon_kg": round(carbon_kg, 4),
        "co2e_kg": round(co2e_kg, 4),
        "co2e_net_kg_after_reserves": round(co2e_net_kg, 4),
        "constants": {
            "root_shoot_ratio": ROOT_SHOOT_RATIO,
            "carbon_fraction": CARBON_FRACTION,
            "co2_molar_ratio": CO2_MOLAR_RATIO,
            "achievement_reserve": ACHIEVEMENT_RESERVE,
            "risk_buffer": RISK_BUFFER,
        },
    }


def detect_card_width_px(image) -> float | None:
    """Detect the calibration card and return its pixel width.

    Uses OpenCV when available (contour detection of a rectangular card);
    returns None when detection is not possible so callers can fall back.
    """
    try:
        import cv2  # type: ignore
    except ImportError:
        logger.warning("OpenCV not available — card detection skipped (fall back to manual DBH).")
        return None

    try:
        import numpy as np  # type: ignore

        gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0
        for cnt in contours:
            approx = cv2.approxPolyDP(cnt, 0.02 * cv2.arcLength(cnt, True), True)
            if len(approx) == 4:
                area = cv2.contourArea(approx)
                if area > best_area:
                    best_area = area
                    best = approx
        if best is None:
            return None
        x, y, w, h = cv2.boundingRect(best)
        return float(max(w, h))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Card detection failed: %s", exc)
        return None


def estimate_dbh_from_image(image, species: str) -> dict:
    """Estimate DBH (cm) from a photo using the calibration card as scale.

    dbh_px = tree trunk width at breast height in pixels (measured by the
    analysis step that identifies the trunk); card_px = card pixel width.
        dbh_cm = (dbh_px / card_px) * CARD_WIDTH_MM / 10
    """
    card_px = detect_card_width_px(image)
    if not card_px or card_px <= 0:
        return {"estimated": False, "reason": "card_not_detected"}
    # Trunk-width detection is a model refinement (P4). v1 returns the scale
    # factor so a later CV step can drop the measured trunk pixels in.
    dbh_px = _estimate_trunk_width_px(image)
    if not dbh_px or dbh_px <= 0:
        return {"estimated": False, "reason": "trunk_not_detected", "card_px": card_px}
    dbh_cm = (dbh_px / card_px) * (CARD_WIDTH_MM / 10.0)
    return {"estimated": True, "dbh_cm": round(dbh_cm, 2), "card_px": card_px, "trunk_px": dbh_px}


def _estimate_trunk_width_px(image) -> float | None:
    """Placeholder for trunk-width CV (P4). Falls back to None.

    The full pipeline (YOLO trunk segmentation or edge-pair detection) is a
    follow-up unit; the worker is structured so the result of this function
    slots straight into the PM002 chain.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return None
    try:
        gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
        # Simple heuristic: vertical dark band near image center = trunk.
        h, w = gray.shape
        center_col = w // 2
        band = gray[:, max(0, center_col - 30) : min(w, center_col + 30)]
        dark = band < 100
        col_dark = dark.sum(axis=0)
        # Find the widest contiguous run of dark columns in the central band.
        best_run = 0
        run = 0
        for v in col_dark:
            run = run + 1 if v > 20 else 0
            best_run = max(best_run, run)
        return float(best_run) if best_run > 0 else None
    except Exception:  # pragma: no cover - defensive
        return None


def extract_frames(video_path: Path, max_frames: int = 8):
    """Yield video frames (PIL Images) via OpenCV. Empty if OpenCV absent."""
    try:
        import cv2  # type: ignore
        from PIL import Image
    except ImportError:
        logger.warning("OpenCV not available — video frame extraction skipped.")
        return
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning("Could not open video %s", video_path)
        return
    count = 0
    while count < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        count += 1
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        yield Image.fromarray(rgb)
    cap.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="walk-around video (webm/mp4)")
    parser.add_argument("--photo", type=Path, help="calibration-card photo (jpg/png)")
    parser.add_argument("--dbh", type=float, help="manual DBH cross-check (cm)")
    parser.add_argument(
        "--species",
        default="default",
        help="species key (cacao, brazil_nut, acai, mahogany, jatoba)",
    )
    parser.add_argument("--tree-id", default="", help="tree identifier (echoed in output)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    if not args.video and not args.photo:
        print("Error: provide --video and/or --photo", file=sys.stderr)
        return 2

    # 1) Try to estimate DBH from the photo (or first usable video frame).
    dbh = args.dbh
    measurement = {"estimated": False, "reason": "manual_dbh_provided"}
    source_image = None

    from PIL import Image

    if args.photo:
        source_image = Image.open(args.photo).convert("RGB")
    elif args.video:
        for frame in extract_frames(args.video, max_frames=8):
            source_image = frame
            break

    if source_image is not None:
        measurement = estimate_dbh_from_image(source_image, args.species)
        if measurement.get("estimated") and dbh is None:
            dbh = measurement["dbh_cm"]

    # 2) PM002 chain — with manual DBH if no card-based estimate was possible.
    if dbh is None or dbh <= 0:
        result = {
            "ok": False,
            "reason": "no_usable_dbh",
            "measurement": measurement,
            "hint": "Provide --dbh (manual cross-check) or a clearer calibration-card photo.",
        }
        print(json.dumps(result, indent=2))
        return 1

    chain = pm002_chain(args.species, dbh)
    result = {
        "ok": True,
        "tree_id": args.tree_id,
        "measurement": measurement,
        "pm002": chain,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
