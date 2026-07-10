"""Geometric orographic-wind heuristic (no external binary).

Downscales the coarse model wind onto a finer grid over the bbox and applies:
- Lee wind shadow: a downwind reduction region behind each island of length
  L ~ k*h (peak height h), with recovery over distance and lateral falloff.
- Corner / headland acceleration: a modest speed-up abreast of island edges.

Over open water (far from any island) the correction is ~0, so the field matches
the raw model there. This is the fallback for when WindNinja isn't installed, and
a fast preview engine.
"""

from __future__ import annotations

import math

import numpy as np

from ..geo import LandMask
from ..metocean import MS_TO_KN, WindField
from ..terrain import TerrainProvider

_SHADOW_MIN = 0.35  # deepest lee reduction (35% of open-water speed)
_CORNER_GAIN = 1.10
_DEFAULT_PEAK_M = 300.0


def _nm_offset(from_lat, from_lon, to_lat, to_lon) -> tuple[float, float]:
    """Approx (east_nm, north_nm) from one point to another."""
    north = (to_lat - from_lat) * 60.0
    east = (to_lon - from_lon) * 60.0 * math.cos(math.radians(0.5 * (from_lat + to_lat)))
    return east, north


class HeuristicTerrainWind:
    def apply(
        self,
        wind: WindField,
        land: LandMask,
        terrain: TerrainProvider,
        refine_n: int = 24,
    ) -> WindField:
        if land.is_empty:
            return wind  # nothing to downscale onto

        lats = np.linspace(float(wind.lats[0]), float(wind.lats[-1]), refine_n)
        lons = np.linspace(float(wind.lons[0]), float(wind.lons[-1]), refine_n)
        nt = wind.n_times
        import datetime as _dt

        times = [_dt.datetime.fromtimestamp(t, _dt.UTC) for t in wind.times]

        # Precompute island descriptors + peak heights.
        islands = []
        for clat, clon, r_nm in land.islands():
            h = terrain.elevation(clat, clon)
            peak = _DEFAULT_PEAK_M if not h or h <= 0 else max(h, 50.0)
            l_nm = float(min(15.0, max(2.0, peak * 0.02)))
            islands.append((clat, clon, max(0.5, r_nm), l_nm))

        u_out = np.zeros((nt, refine_n, refine_n))
        v_out = np.zeros((nt, refine_n, refine_n))

        for k in range(nt):
            t = times[k]
            for i, la in enumerate(lats):
                for j, lo in enumerate(lons):
                    uv = wind.uv_at(float(la), float(lo), t)
                    if uv is None:
                        continue
                    u, v = uv
                    spd = math.hypot(u, v)
                    if spd < 1e-6:
                        u_out[k, i, j] = u
                        v_out[k, i, j] = v
                        continue
                    toward_e, toward_n = u / spd, v / spd  # unit downwind vector
                    scale = 1.0
                    for clat, clon, r_nm, l_nm in islands:
                        ex, ny = _nm_offset(clat, clon, la, lo)
                        # Along = downwind distance from centroid; lateral = cross-wind.
                        along = ex * toward_e + ny * toward_n
                        lateral = abs(-ex * toward_n + ny * toward_e)
                        # Shadow starts at the island's downwind edge (~r_nm) and
                        # recovers over L beyond it.
                        edge = along - r_nm
                        if 0.0 <= edge < l_nm and lateral < r_nm:
                            recover = edge / l_nm
                            lat_fall = max(0.0, 1.0 - lateral / r_nm)
                            reduction = _SHADOW_MIN + (1 - _SHADOW_MIN) * recover
                            scale = min(scale, reduction * lat_fall + (1 - lat_fall))
                        elif abs(along) <= r_nm and r_nm <= lateral < r_nm * 1.6:
                            scale = max(scale, _CORNER_GAIN)
                    u_out[k, i, j] = u * scale
                    v_out[k, i, j] = v * scale

        return WindField(
            model_id=f"{wind.model_id}+terrain",
            times=np.asarray(wind.times, dtype=float),
            lats=lats,
            lons=lons,
            u10=u_out,
            v10=v_out,
        )

    @staticmethod
    def _tws_kn(u: float, v: float) -> float:
        return math.hypot(u, v) * MS_TO_KN
