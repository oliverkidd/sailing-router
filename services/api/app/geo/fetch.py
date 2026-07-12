"""Download and cache land / depth data for a course bounding box."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from ..config import DATA_DIR, GEO_CACHE_DIR
from ..schemas import BBox, Course
from . import (
    DepthField,
    GeoContext,
    LandMask,
    course_bbox,
)
from .sources import (
    fetch_depth_grid,
    gshhg_land,
    require_gshhg,
)

logger = logging.getLogger(__name__)

# Same EMODnet mosaic used for depth.pmtiles isobaths (chart region).
_REGION_DEPTH_TIF = DATA_DIR / "geo" / "bathymetry" / "region_depth.tif"
_region_depth_ds = None


def _bbox_key(bbox: BBox) -> str:
    return bbox.key()


def fetch_land_for_bbox(bbox: BBox, *, require_hires: bool = False) -> LandMask:
    key = _bbox_key(bbox)
    # Chart baseline uses a dedicated cache file so it never mixes with NE fallbacks.
    out_path = (
        GEO_CACHE_DIR / f"{key}_land_chart.geojson"
        if require_hires
        else GEO_CACHE_DIR / f"{key}_land.geojson"
    )
    if out_path.exists():
        return LandMask.from_geojson(out_path)

    if require_hires:
        require_gshhg()
        polys = gshhg_land(bbox, allow_fallback=False)
    else:
        polys = gshhg_land(bbox, allow_fallback=True)

    if not polys:
        return LandMask([])

    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": json.loads(json.dumps(p.__geo_interface__)),
        }
        for p in polys
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return LandMask.from_geojson(out_path)


def fetch_depth_for_bbox(bbox: BBox, land: LandMask) -> DepthField:
    key = _bbox_key(bbox)
    npz_path = GEO_CACHE_DIR / f"{key}_depth.npz"
    if npz_path.exists():
        z = np.load(npz_path)
        return DepthField(z["lats"], z["lons"], z["depth_m"])

    polys = land.polygons
    lats, lons, depth = fetch_depth_grid(bbox, polys)
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(npz_path, lats=lats, lons=lons, depth_m=depth)
    return DepthField(lats, lons, depth)


def ensure_geo_context(course: Course, margin_deg: float = 0.5) -> GeoContext:
    bbox = course_bbox(course, margin_deg=margin_deg)
    land = fetch_land_for_bbox(bbox, require_hires=False)
    depth = fetch_depth_for_bbox(bbox, land)
    return GeoContext.build(
        land=land,
        depth=depth,
        exclusion_rings=[[tuple(p) for p in ring] for ring in course.exclusions],
        min_depth_m=course.min_depth_m,
    )


def _bbox_from_cache_stem(stem: str) -> BBox | None:
    parts = stem.split("_")
    if len(parts) != 4:
        return None
    try:
        lat_min, lon_min, lat_max, lon_max = map(float, parts)
    except ValueError:
        return None
    return BBox(lat_min=lat_min, lon_min=lon_min, lat_max=lat_max, lon_max=lon_max)


def _covering_depth_npz(lat: float, lon: float) -> Path | None:
    best: Path | None = None
    best_area = float("inf")
    for npz_path in GEO_CACHE_DIR.glob("*_depth.npz"):
        bbox = _bbox_from_cache_stem(npz_path.stem.removesuffix("_depth"))
        if bbox is None:
            continue
        if lat < bbox.lat_min or lat > bbox.lat_max or lon < bbox.lon_min or lon > bbox.lon_max:
            continue
        area = (bbox.lat_max - bbox.lat_min) * (bbox.lon_max - bbox.lon_min)
        if area < best_area:
            best_area = area
            best = npz_path
    return best


def _covering_land_geojson(lat: float, lon: float) -> Path | None:
    best: Path | None = None
    best_area = float("inf")
    for land_path in GEO_CACHE_DIR.glob("*_land*.geojson"):
        stem = land_path.stem
        for suffix in ("_land_chart", "_land"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            continue
        bbox = _bbox_from_cache_stem(stem)
        if bbox is None:
            continue
        if lat < bbox.lat_min or lat > bbox.lat_max or lon < bbox.lon_min or lon > bbox.lon_max:
            continue
        area = (bbox.lat_max - bbox.lat_min) * (bbox.lon_max - bbox.lon_min)
        if area < best_area:
            best_area = area
            best = land_path
    return best


def _sample_region_depth_tif(lat: float, lon: float) -> float | None:
    """Point depth (m, positive-down) from the chart EMODnet mosaic, or None.

    Returns None for out-of-bounds, land, or nodata. Use
    `_in_region_depth_bounds` to tell those cases apart from “no mosaic file”.
    """
    global _region_depth_ds
    if not _REGION_DEPTH_TIF.exists():
        return None
    try:
        import rasterio  # noqa: PLC0415
        import rasterio.windows  # noqa: PLC0415

        if _region_depth_ds is None:
            _region_depth_ds = rasterio.open(_REGION_DEPTH_TIF)
        ds = _region_depth_ds
        left, bottom, right, top = ds.bounds
        if lon < left or lon > right or lat < bottom or lat > top:
            return None
        row, col = ds.index(lon, lat)
        if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
            return None
        val = float(ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0])
        if not np.isfinite(val) or val <= 0:
            return None  # land / nodata
        return val
    except Exception as exc:  # noqa: BLE001
        logger.debug("region_depth.tif sample failed: %s", exc)
        return None


def _in_region_depth_bounds(lat: float, lon: float) -> bool:
    """True when (lat, lon) lies inside the chart bathymetry mosaic footprint."""
    global _region_depth_ds
    if not _REGION_DEPTH_TIF.exists():
        return False
    try:
        import rasterio  # noqa: PLC0415

        if _region_depth_ds is None:
            _region_depth_ds = rasterio.open(_REGION_DEPTH_TIF)
        left, bottom, right, top = _region_depth_ds.bounds
        return left <= lon <= right and bottom <= lat <= top
    except Exception:  # noqa: BLE001
        return False


def depth_at_point(lat: float, lon: float) -> float | None:
    """Water depth in metres — prefer chart mosaic, then cached depth grids."""
    mosaic = _sample_region_depth_tif(lat, lon)
    if mosaic is not None:
        return mosaic
    # Inside the chart mosaic: don't fall back to stale heuristic NPZs (often capped at 500 m).
    if _in_region_depth_bounds(lat, lon):
        return None

    npz_path = _covering_depth_npz(lat, lon)
    if npz_path is None:
        return None
    z = np.load(npz_path)
    depth = DepthField(z["lats"], z["lons"], z["depth_m"]).depth((lat, lon))
    if not np.isfinite(depth) or depth <= 0 or depth >= 9000.0:
        return None
    return depth


# Shallowest chart isobath — mosaic nodata over water means "shallower than this".
CHART_MIN_ISOBATH_M = 2.0


def depth_lt_at_point(
    lat: float,
    lon: float,
    *,
    on_land: bool | None,
    depth_m: float | None = None,
) -> float | None:
    """When depth is unknown but the click is wet inside the chart mosaic, return <N m."""
    if depth_m is not None or on_land is True:
        return None
    if not _in_region_depth_bounds(lat, lon):
        return None
    return CHART_MIN_ISOBATH_M


def on_land_at_point(lat: float, lon: float) -> bool | None:
    """True/false when a cached land mask covers the point; None when unknown."""
    land_path = _covering_land_geojson(lat, lon)
    if land_path is None:
        return None
    return LandMask.from_geojson(land_path).on_land((lat, lon))


def elevation_at_point(lat: float, lon: float) -> float | None:
    """Elevation in metres: DEM when available, otherwise -depth over water."""
    from ..terrain import get_terrain

    terrain = get_terrain()
    if terrain.available:
        return terrain.elevation(lat, lon)

    depth = depth_at_point(lat, lon)
    if depth is None:
        return None
    on_land = on_land_at_point(lat, lon)
    if on_land:
        return None
    return -depth
