"""Shared UK+Europe chart region for tile builds and app maxBounds."""

from __future__ import annotations

import os
import sys

# lon -25..42, lat 30..72 — UK, Ireland, Scandinavia, W Europe, Med.
REGION_BBOX = {
    "lon_min": -25.0,
    "lat_min": 30.0,
    "lon_max": 42.0,
    "lat_max": 72.0,
}

# Isle of Wight / Solent — style preview before full-region Overpass crawl.
SAMPLE_BBOX = {
    "lon_min": -1.75,
    "lat_min": 50.55,
    "lon_max": -0.95,
    "lat_max": 50.95,
}

# Isobath levels (metres, positive-down) for depth.pmtiles.
# Includes 0.5 / 1 m for harbour-scale continuity (soft_noclip_05_1m).
DEPTH_LEVELS_M = (0.5, 1, 2, 4, 5, 10, 20, 50, 100, 200)

# EMODnet DTM native resolution ≈ 1/16 arc-minute.
EMODNET_RES_DEG = 1.0 / 960.0


def use_sample() -> bool:
    """True when CHART_SAMPLE=1 or --sample is on the CLI."""
    if os.environ.get("CHART_SAMPLE") == "1":
        return True
    return "--sample" in sys.argv


def chart_bbox() -> dict[str, float]:
    """Active build bbox: Solent sample or full REGION_BBOX."""
    return SAMPLE_BBOX if use_sample() else REGION_BBOX
