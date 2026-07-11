"""Download and cache land / depth data for a course bounding box."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..config import GEO_CACHE_DIR
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
)


def _bbox_key(bbox: BBox) -> str:
    return bbox.key()


def fetch_land_for_bbox(bbox: BBox) -> LandMask:
    key = _bbox_key(bbox)
    cached = GEO_CACHE_DIR / f"{key}_land.geojson"
    if cached.exists():
        return LandMask.from_geojson(cached)

    polys = gshhg_land(bbox)
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
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return LandMask.from_geojson(cached)


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
    land = fetch_land_for_bbox(bbox)
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
    for land_path in GEO_CACHE_DIR.glob("*_land.geojson"):
        bbox = _bbox_from_cache_stem(land_path.stem.removesuffix("_land"))
        if bbox is None:
            continue
        if lat < bbox.lat_min or lat > bbox.lat_max or lon < bbox.lon_min or lon > bbox.lon_max:
            continue
        area = (bbox.lat_max - bbox.lat_min) * (bbox.lon_max - bbox.lon_min)
        if area < best_area:
            best_area = area
            best = land_path
    return best


def depth_at_point(lat: float, lon: float) -> float | None:
    """Water depth in metres from the smallest cached depth grid covering the point."""
    npz_path = _covering_depth_npz(lat, lon)
    if npz_path is None:
        return None
    z = np.load(npz_path)
    depth = DepthField(z["lats"], z["lons"], z["depth_m"]).depth((lat, lon))
    if depth >= 9000.0:
        return None
    return depth


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
