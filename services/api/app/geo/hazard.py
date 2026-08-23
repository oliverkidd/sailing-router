"""Hazard reward/punish grid: soft time cost near shore and shoals.

Unused by IsochroneRouter (product: depth is a hard min+margin cutoff only).
Kept for offline experiments.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from ..schemas import LatLon
from . import DepthField, LandMask


@dataclass
class HazardGrid:
    """Coarse cost field over a course bbox. cost units ≈ seconds of lost time."""

    lats: np.ndarray
    lons: np.ndarray
    cost_s: np.ndarray  # same shape as depth cells

    def cost_at(self, pos: LatLon) -> float:
        lat, lon = pos
        if (
            lat < self.lats[0]
            or lat > self.lats[-1]
            or lon < self.lons[0]
            or lon > self.lons[-1]
            or self.lats.size < 2
            or self.lons.size < 2
        ):
            return 0.0
        i = int(np.clip(np.searchsorted(self.lats, lat) - 1, 0, self.lats.size - 2))
        j = int(np.clip(np.searchsorted(self.lons, lon) - 1, 0, self.lons.size - 2))
        la0, la1 = self.lats[i], self.lats[i + 1]
        lo0, lo1 = self.lons[j], self.lons[j + 1]
        fy = 0.0 if la1 == la0 else (lat - la0) / (la1 - la0)
        fx = 0.0 if lo1 == lo0 else (lon - lo0) / (lo1 - lo0)
        c0 = self.cost_s[i, j] * (1 - fx) + self.cost_s[i, j + 1] * fx
        c1 = self.cost_s[i + 1, j] * (1 - fx) + self.cost_s[i + 1, j + 1] * fx
        return float(max(0.0, c0 * (1 - fy) + c1 * fy))


def build_hazard_grid(
    depth: DepthField,
    land: LandMask,
    *,
    min_depth_m: float,
    safety_margin_m: float = 1.0,
    shore_weight_s: float = 30.0,
    shoal_weight_s: float = 35.0,
    # Only penalise within this many metres of land/shoal.
    shore_influence_m: float = 150.0,
    max_cost_s: float = 40.0,
    stride: int = 4,
) -> HazardGrid:
    """Build a coarsened hazard cost grid from chart depth + land.

    Cost is zero in navigable open water / channel midlines; it only rises
    within ``shore_influence_m`` of land/shoal or when depth margin is thin.
    """
    lats = depth.lats[::stride]
    lons = depth.lons[::stride]
    d = depth.depth_m[::stride, ::stride].astype(np.float64)
    if lats.size < 2 or lons.size < 2:
        return HazardGrid(lats=depth.lats[:2], lons=depth.lons[:2], cost_s=np.zeros((2, 2)))

    threshold = min_depth_m + safety_margin_m
    wet = (d >= threshold) & np.isfinite(d)
    if not land.is_empty:
        for i, la in enumerate(lats):
            for j, lo in enumerate(lons):
                if land.on_land((float(la), float(lo))):
                    wet[i, j] = False
                    d[i, j] = 0.0

    dist_cells = ndimage.distance_transform_edt(wet)
    # Approximate metres per cell from lat spacing.
    if lats.size >= 2:
        cell_m = abs(float(lats[1] - lats[0])) * 111_000.0
    else:
        cell_m = 100.0
    dist_m = dist_cells * cell_m

    # Shore term: only inside the influence radius.
    shore = np.where(
        wet & (dist_m < shore_influence_m),
        shore_weight_s * (1.0 - dist_m / shore_influence_m) ** 2,
        0.0,
    )
    shore = np.where(wet, shore, max_cost_s)

    # Shoal term: only when depth margin is thin (< 4 m above threshold).
    margin = d - threshold
    shoal = np.where(
        wet & (margin < 4.0),
        shoal_weight_s * (1.0 - np.clip(margin, 0.0, 4.0) / 4.0) ** 2,
        0.0,
    )
    shoal = np.where(wet, shoal, max_cost_s)

    cost = np.clip(shore + shoal, 0.0, max_cost_s)
    cost = np.where(cost < 1.5, 0.0, cost)

    return HazardGrid(lats=lats.astype(np.float64), lons=lons.astype(np.float64), cost_s=cost)
