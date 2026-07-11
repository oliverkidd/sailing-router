"""LOD tile alignment and grid-dimension helpers for wind overlays."""

from __future__ import annotations

import math

from ..schemas import BBox

# Fixed tile grid size (points per edge). 32x32 = 1024 pts — safe for Open-Meteo.
TILE_N = 32

# LOD band spacing in degrees (clamped up to model.resolution_deg at fetch time).
LOD_SPACING = {
    0: 0.2,  # overview — tile ≈ 6.4°
    1: 0.05,  # local — tile ≈ 1.6°
    2: None,  # detail — use model native resolution
}

_ROUTE_GRID_N = 8
_MIN_GRID_N = 4
_MAX_GRID_N = 32


def lod_spacing(lod: int, model_resolution_deg: float) -> float:
    """Effective grid spacing for an LOD band, never finer than native."""
    band = LOD_SPACING.get(lod, LOD_SPACING[0])
    if band is None:
        return max(0.01, float(model_resolution_deg))
    return max(float(band), float(model_resolution_deg))


def tile_size_deg(lod: int, model_resolution_deg: float) -> float:
    """Edge length of one LOD tile in degrees."""
    return TILE_N * lod_spacing(lod, model_resolution_deg)


def align_tile_bbox(bbox: BBox) -> BBox:
    """Snap tile corners to a stable 3-decimal grid for cache-key reuse."""
    return BBox(
        lat_min=round(bbox.lat_min, 3),
        lon_min=round(bbox.lon_min, 3),
        lat_max=round(bbox.lat_max, 3),
        lon_max=round(bbox.lon_max, 3),
    )


def clamp_grid_dims(
    bbox: BBox,
    nx: int,
    ny: int,
    model_resolution_deg: float,
) -> tuple[int, int]:
    """Clamp requested dims so spacing is never finer than model native resolution."""
    lat_span = max(1e-6, bbox.lat_max - bbox.lat_min)
    lon_span = max(1e-6, bbox.lon_max - bbox.lon_min)
    res = max(0.01, float(model_resolution_deg))

    max_ny = max(_MIN_GRID_N, int(math.floor(lat_span / res)) + 1)
    max_nx = max(_MIN_GRID_N, int(math.floor(lon_span / res)) + 1)

    nlat = int(max(_MIN_GRID_N, min(_MAX_GRID_N, ny, max_ny)))
    nlon = int(max(_MIN_GRID_N, min(_MAX_GRID_N, nx, max_nx)))
    return nlat, nlon


def route_grid_dims(bbox: BBox, model_resolution_deg: float) -> tuple[int, int]:
    """Modest native-clamped grid for multi-day routing fetches."""
    return clamp_grid_dims(bbox, _ROUTE_GRID_N, _ROUTE_GRID_N, model_resolution_deg)
