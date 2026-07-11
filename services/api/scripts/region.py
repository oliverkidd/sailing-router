"""Shared UK+Europe chart region for tile builds and app maxBounds."""

from __future__ import annotations

# lon -25..42, lat 30..72 — UK, Ireland, Scandinavia, W Europe, Med.
REGION_BBOX = {
    "lon_min": -25.0,
    "lat_min": 30.0,
    "lon_max": 42.0,
    "lat_max": 72.0,
}

# Isobath levels (metres, positive-down) for depth.pmtiles.
# 2 m steps through 30 m, then deep reference lines.
DEPTH_LEVELS_M = (
    2,
    4,
    6,
    8,
    10,
    12,
    14,
    16,
    18,
    20,
    22,
    24,
    26,
    28,
    30,
    50,
    100,
    200,
)

# EMODnet DTM native resolution ≈ 1/16 arc-minute.
EMODNET_RES_DEG = 1.0 / 960.0
