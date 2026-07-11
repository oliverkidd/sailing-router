"""Generate depth isobath LineStrings from a DepthField (offline / tools)."""

from __future__ import annotations

from typing import Any

import numpy as np
from shapely.geometry import LineString, mapping
from shapely.ops import unary_union

from . import DepthField

DEFAULT_LEVELS_M = (2.0, 5.0, 10.0, 20.0, 30.0, 50.0, 100.0, 200.0)

_SIMPLIFY_TOL_DEG = 0.0004
_MIN_RING_POINTS = 4


def _index_to_lonlat(
    row: float, col: float, lats: np.ndarray, lons: np.ndarray
) -> tuple[float, float]:
    """Bilinear map of fractional grid indices to (lon, lat)."""
    nlat, nlon = lats.size, lons.size
    r0 = int(np.clip(np.floor(row), 0, max(0, nlat - 2)))
    c0 = int(np.clip(np.floor(col), 0, max(0, nlon - 2)))
    fr = float(row) - r0
    fc = float(col) - c0
    if nlat == 1:
        lat = float(lats[0])
    else:
        lat = float(lats[r0] * (1 - fr) + lats[min(r0 + 1, nlat - 1)] * fr)
    if nlon == 1:
        lon = float(lons[0])
    else:
        lon = float(lons[c0] * (1 - fc) + lons[min(c0 + 1, nlon - 1)] * fc)
    return lon, lat


def depth_contours(
    depth: DepthField,
    levels: list[float] | tuple[float, ...] | None = None,
    *,
    max_depth_m: float = 200.0,
    land_polys: list | None = None,
) -> dict[str, Any]:
    """Emit LineString isobaths at the given depth levels, optionally land-clipped.

    Chart baseline now uses pre-baked PMTiles; this remains for tests and tools.
    """
    from skimage.measure import find_contours  # noqa: PLC0415

    if levels is None:
        levels = [lv for lv in DEFAULT_LEVELS_M if lv <= max_depth_m]
    else:
        levels = [float(lv) for lv in levels if float(lv) <= max_depth_m]

    arr = np.asarray(depth.depth_m, dtype=float)
    arr = np.where(np.isfinite(arr), arr, 0.0)
    arr = np.clip(arr, 0.0, 10_000.0)
    lats = np.asarray(depth.lats, dtype=float)
    lons = np.asarray(depth.lons, dtype=float)

    land_union = None
    if land_polys:
        try:
            land_union = unary_union(land_polys)
        except Exception:  # noqa: BLE001
            land_union = None

    features: list[dict[str, Any]] = []
    if arr.shape[0] < 2 or arr.shape[1] < 2:
        return {"type": "FeatureCollection", "features": features}

    for level in levels:
        try:
            contours = find_contours(arr, level)
        except Exception:  # noqa: BLE001
            continue
        for contour in contours:
            if contour.shape[0] < _MIN_RING_POINTS:
                continue
            coords = [
                _index_to_lonlat(float(r), float(c), lats, lons) for r, c in contour
            ]
            try:
                line = LineString(coords)
            except Exception:  # noqa: BLE001
                continue
            if line.is_empty or line.length == 0:
                continue
            if land_union is not None and not land_union.is_empty:
                try:
                    line = line.difference(land_union)
                except Exception:  # noqa: BLE001
                    pass
            if line.is_empty:
                continue
            geoms = (
                [line]
                if line.geom_type == "LineString"
                else list(getattr(line, "geoms", []))
            )
            for g in geoms:
                if g.is_empty or g.geom_type != "LineString" or len(g.coords) < 2:
                    continue
                simplified = g.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=False)
                if simplified.is_empty or simplified.geom_type != "LineString":
                    continue
                features.append(
                    {
                        "type": "Feature",
                        "properties": {"depth_m": level, "depth": int(level)},
                        "geometry": mapping(simplified),
                    }
                )

    return {"type": "FeatureCollection", "features": features}
