"""Bounding-box + canonical-grid helpers for the GRIB wind pipeline."""

from __future__ import annotations

import math

import numpy as np

from ..schemas import BBox

# ~500 m at mid-latitudes — store + query resolution (canonical grid).
CANON_RES_DEG = 0.0045
# Alias kept for display-budget callers.
TARGET_RES_DEG = CANON_RES_DEG
MAX_DISPLAY_PTS = 96 * 96
MAX_CANON_PTS = 600 * 600

REGION_PAD_DEG = 2.0
REGION_SNAP_DEG = 2.0


def align_tile_bbox(bbox: BBox) -> BBox:
    """Snap corners to a stable 3-decimal grid for cache-key reuse."""
    return BBox(
        lat_min=round(bbox.lat_min, 3),
        lon_min=round(bbox.lon_min, 3),
        lat_max=round(bbox.lat_max, 3),
        lon_max=round(bbox.lon_max, 3),
    )


def region_bbox(
    bbox: BBox,
    pad_deg: float = REGION_PAD_DEG,
    snap_deg: float = REGION_SNAP_DEG,
) -> BBox:
    """Expand bbox by pad and snap outward to snap_deg grid for cache sharing."""
    lat_min = math.floor((bbox.lat_min - pad_deg) / snap_deg) * snap_deg
    lon_min = math.floor((bbox.lon_min - pad_deg) / snap_deg) * snap_deg
    lat_max = math.ceil((bbox.lat_max + pad_deg) / snap_deg) * snap_deg
    lon_max = math.ceil((bbox.lon_max + pad_deg) / snap_deg) * snap_deg
    if lat_max <= lat_min:
        lat_max = lat_min + snap_deg
    if lon_max <= lon_min:
        lon_max = lon_min + snap_deg
    return BBox(
        lat_min=round(lat_min, 4),
        lon_min=round(lon_min, 4),
        lat_max=round(lat_max, 4),
        lon_max=round(lon_max, 4),
    )


def canonical_grid(region: BBox) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic ascending (lats, lons) on a GLOBAL CANON_RES_DEG lattice.

    Snapping to the global lattice (not to region.lat_min) guarantees identical
    axes whenever the same region is requested again → concat_time + terrain stay
    aligned. If the region is too large for MAX_CANON_PTS at 500 m, the spacing
    is doubled (still a multiple of CANON_RES_DEG) until it fits — deterministic
    for a given region.
    """
    res = CANON_RES_DEG
    while True:
        lat0 = math.floor(region.lat_min / res) * res
        lon0 = math.floor(region.lon_min / res) * res
        nlat = max(2, int(round((region.lat_max - lat0) / res)) + 1)
        nlon = max(2, int(round((region.lon_max - lon0) / res)) + 1)
        if nlat * nlon <= MAX_CANON_PTS:
            lats = lat0 + np.arange(nlat, dtype=float) * res
            lons = lon0 + np.arange(nlon, dtype=float) * res
            return lats, lons
        res *= 2.0
        if res > 2.0:
            raise ValueError(
                f"region too large for canonical grid: "
                f"{region.lat_min},{region.lon_min}–{region.lat_max},{region.lon_max}"
            )


def budget_dims(
    bbox: BBox,
    target_res: float = TARGET_RES_DEG,
    max_pts: int = MAX_DISPLAY_PTS,
) -> tuple[int, int]:
    """Dims at target_res, shrunk under max_pts while keeping aspect."""
    lat_span = max(1e-6, bbox.lat_max - bbox.lat_min)
    lon_span = max(1e-6, bbox.lon_max - bbox.lon_min)
    res = max(1e-6, float(target_res))
    nlat = max(2, int(math.ceil(lat_span / res)) + 1)
    nlon = max(2, int(math.ceil(lon_span / res)) + 1)
    while nlat * nlon > max_pts and (nlat > 2 or nlon > 2):
        if nlat >= nlon and nlat > 2:
            nlat -= 1
        elif nlon > 2:
            nlon -= 1
        else:
            break
    return nlat, nlon


def hourly_epochs(start, end) -> list[float]:
    """Whole-hour UTC epochs covering [start, end] inclusive."""
    start_e = start.timestamp() if hasattr(start, "timestamp") else float(start)
    end_e = end.timestamp() if hasattr(end, "timestamp") else float(end)
    lo = math.floor(start_e / 3600) * 3600
    hi = math.ceil(end_e / 3600) * 3600
    if hi < lo:
        hi = lo
    return list(np.arange(lo, hi + 1, 3600, dtype=float))
