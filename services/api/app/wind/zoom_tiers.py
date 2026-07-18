"""Zoom-tiered display resolution + window pad for /wind/area."""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import BBox
from ..util.bbox import REGION_PAD_DEG

# Modelling substrate (200 m at mid-latitudes).
WIND_CANON_RES_DEG = 0.0018

# Per-tier on-wire point budgets (display only; forcing cache is separate).
_TIER_MAX_PTS = {
    "coarse": 128 * 128,
    "mid": 192 * 192,
    "fine": 256 * 256,
    "ultra": 320 * 320,
}


@dataclass(frozen=True)
class ZoomTier:
    name: str
    # Target grid spacing for the display series (degrees).
    target_res_deg: float
    # Pad added to the viewport for the returned display window.
    window_pad_deg: float
    max_pts: int


def tier_for_zoom(zoom: float | None) -> ZoomTier:
    """Map MapLibre zoom to a display tier.

    Terrain downscaling always applies when a library is present; only density
    and window pad change with zoom.
    """
    z = 5.0 if zoom is None else float(zoom)
    if z <= 6:
        return ZoomTier("coarse", 0.06, REGION_PAD_DEG, _TIER_MAX_PTS["coarse"])
    if z <= 9:
        return ZoomTier("mid", 0.015, 1.0, _TIER_MAX_PTS["mid"])
    if z <= 12:
        return ZoomTier("fine", 0.0045, 0.5, _TIER_MAX_PTS["fine"])
    return ZoomTier("ultra", WIND_CANON_RES_DEG, 0.25, _TIER_MAX_PTS["ultra"])


def display_window(viewport: BBox, zoom: float | None = None) -> BBox:
    """Expand the client viewport by the zoom-tier pad (no snap — display only)."""
    tier = tier_for_zoom(zoom)
    pad = tier.window_pad_deg
    return BBox(
        lat_min=round(viewport.lat_min - pad, 4),
        lon_min=round(viewport.lon_min - pad, 4),
        lat_max=round(viewport.lat_max + pad, 4),
        lon_max=round(viewport.lon_max + pad, 4),
    )
