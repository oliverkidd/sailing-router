"""Download and cache land / depth data for a course bounding box.

Routing uses the SAME chart sources as the map:
  - depth: data/geo/bathymetry/region_depth.tif (EMODnet mosaic behind depth.pmtiles)
  - land:  OSM land polygons (same as coastline.pmtiles), cached per bbox
"""

from __future__ import annotations

import json
import logging
import subprocess
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

logger = logging.getLogger(__name__)

# Same EMODnet mosaic used for depth.pmtiles isobaths (chart region).
_REGION_DEPTH_TIF = DATA_DIR / "geo" / "bathymetry" / "region_depth.tif"
_region_depth_ds = None

# OSM land FeatureCollection (optional prebuild via scripts/build_route_land.py).
_LAND_CHART_GEOJSON = DATA_DIR / "geo" / "osm" / "land_chart.geojson"
# Coastline-build intermediate (GeoJSONSeq) — same OSM polygons as the map.
_LAND_GEOJSONL = DATA_DIR / "cache" / "tiles_build" / "land.geojsonl"

_OSM_SHP_CANDIDATES = [
    DATA_DIR / "geo" / "osm" / "land-polygons-split-4326" / "land_polygons.shp",
    DATA_DIR / "geo" / "osm" / "land_polygons.shp",
]


class ChartDataUnavailable(RuntimeError):
    """Raised when the single-source chart depth or land data is missing."""


def _bbox_key(bbox: BBox) -> str:
    return bbox.key()


def _find_osm_shp() -> Path | None:
    for p in _OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = sorted(
        (DATA_DIR / "geo" / "osm").rglob("land_polygons.shp"),
        key=lambda p: (0 if "split" in str(p) else 1, str(p)),
    )
    return matches[0] if matches else None


def _write_land_features(out_path: Path, features: list[dict]) -> LandMask:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    return LandMask.from_geojson(out_path)


def _clip_osm_shapefile(bbox: BBox, out_path: Path) -> LandMask:
    shp = _find_osm_shp()
    if shp is None:
        raise ChartDataUnavailable(
            "OSM land polygons missing. Run: python services/api/scripts/fetch_osm_land.py"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    clip = (
        f"{bbox.lon_min} {bbox.lat_min} "
        f"{bbox.lon_max} {bbox.lat_max}"
    )
    cmd = [
        "ogr2ogr",
        "-f",
        "GeoJSON",
        "-clipsrc",
        *clip.split(),
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "COORDINATE_PRECISION=7",
        str(out_path),
        str(shp),
    ]
    logger.info("clipping OSM land to %s", bbox.key())
    subprocess.run(cmd, check=True, capture_output=True)
    return LandMask.from_geojson(out_path)


def _filter_geojsonl_to_bbox(src: Path, bbox: BBox, out_path: Path) -> LandMask:
    """Stream GeoJSONSeq land features that intersect bbox into a FeatureCollection."""
    from shapely.geometry import box, shape  # noqa: PLC0415

    clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    features: list[dict] = []
    with src.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception:  # noqa: BLE001
                continue
            if g.is_empty or not g.intersects(clip):
                continue
            inter = g.intersection(clip)
            if inter.is_empty:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": json.loads(json.dumps(inter.__geo_interface__)),
                }
            )
    return _write_land_features(out_path, features)


def fetch_land_for_bbox(bbox: BBox, *, require_hires: bool = False) -> LandMask:
    """OSM chart land clipped to bbox (same polygons as coastline.pmtiles)."""
    del require_hires  # always chart-grade OSM now
    import time  # noqa: PLC0415

    t0 = time.perf_counter()
    key = _bbox_key(bbox)
    out_path = GEO_CACHE_DIR / f"{key}_land_chart.geojson"
    if out_path.exists() and out_path.stat().st_size > 50:
        land = LandMask.from_geojson(out_path)
        logger.info(
            "land path=cache key=%s ms=%.0f",
            key,
            (time.perf_counter() - t0) * 1000,
        )
        return land

    # Prefer ogr2ogr shapefile clip (~2s) over parsing the 600MB+ FeatureCollection (~30s).
    if _find_osm_shp() is not None:
        try:
            land = _clip_osm_shapefile(bbox, out_path)
            logger.info(
                "land path=ogr2ogr key=%s ms=%.0f",
                key,
                (time.perf_counter() - t0) * 1000,
            )
            return land
        except ChartDataUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("ogr2ogr land clip failed, falling back: %s", exc)

    # Prebuilt chart FeatureCollection (slow cold path — last resort for huge files).
    if _LAND_CHART_GEOJSON.exists() and _LAND_CHART_GEOJSON.stat().st_size > 1000:
        try:
            land = _filter_geojson_file(_LAND_CHART_GEOJSON, bbox, out_path)
            logger.info(
                "land path=geojson key=%s ms=%.0f",
                key,
                (time.perf_counter() - t0) * 1000,
            )
            return land
        except Exception as exc:  # noqa: BLE001
            logger.warning("land_chart.geojson clip failed: %s", exc)

    # Coastline-build GeoJSONSeq (same clip source as the map tiles).
    if _LAND_GEOJSONL.exists() and _LAND_GEOJSONL.stat().st_size > 1000:
        try:
            land = _filter_geojsonl_to_bbox(_LAND_GEOJSONL, bbox, out_path)
            logger.info(
                "land path=geojsonl key=%s ms=%.0f",
                key,
                (time.perf_counter() - t0) * 1000,
            )
            return land
        except Exception as exc:  # noqa: BLE001
            logger.warning("land.geojsonl filter failed: %s", exc)

    raise ChartDataUnavailable(
        f"Failed to build OSM land mask for {bbox.key()}. "
        "Run: python services/api/scripts/fetch_osm_land.py"
    )


def _filter_geojson_file(src: Path, bbox: BBox, out_path: Path) -> LandMask:
    from shapely.geometry import box, shape  # noqa: PLC0415

    clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    gj = json.loads(src.read_text(encoding="utf-8"))
    features: list[dict] = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:  # noqa: BLE001
            continue
        if g.is_empty or not g.intersects(clip):
            continue
        inter = g.intersection(clip)
        if inter.is_empty:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {},
                "geometry": json.loads(json.dumps(inter.__geo_interface__)),
            }
        )
    return _write_land_features(out_path, features)


def _open_region_depth():
    global _region_depth_ds
    if not _REGION_DEPTH_TIF.exists():
        raise ChartDataUnavailable(
            f"Chart bathymetry missing at {_REGION_DEPTH_TIF}. "
            "Run: python services/api/scripts/build_depth_tiles.py"
        )
    import rasterio  # noqa: PLC0415

    if _region_depth_ds is None:
        _region_depth_ds = rasterio.open(_REGION_DEPTH_TIF)
    return _region_depth_ds


def fetch_depth_for_bbox(bbox: BBox, land: LandMask | None = None) -> DepthField:
    """DepthField window from region_depth.tif (single chart source)."""
    del land  # depth comes only from the mosaic; land is a separate mask
    import time  # noqa: PLC0415

    t0 = time.perf_counter()
    key = _bbox_key(bbox)
    npz_path = GEO_CACHE_DIR / f"{key}_depth_chart.npz"
    if npz_path.exists():
        z = np.load(npz_path)
        logger.info(
            "depth path=cache key=%s ms=%.0f",
            key,
            (time.perf_counter() - t0) * 1000,
        )
        return DepthField(z["lats"], z["lons"], z["depth_m"])

    import rasterio.windows  # noqa: PLC0415

    ds = _open_region_depth()
    left, bottom, right, top = ds.bounds
    # Clip request to mosaic footprint.
    west = max(bbox.lon_min, left)
    east = min(bbox.lon_max, right)
    south = max(bbox.lat_min, bottom)
    north = min(bbox.lat_max, top)
    if east <= west or north <= south:
        raise ChartDataUnavailable(
            f"Course bbox {bbox.key()} is outside chart bathymetry "
            f"({left},{bottom})–({right},{top})"
        )

    window = rasterio.windows.from_bounds(west, south, east, north, transform=ds.transform)
    window = window.intersection(rasterio.windows.Window(0, 0, ds.width, ds.height))
    if window.width < 2 or window.height < 2:
        raise ChartDataUnavailable(f"Depth window too small for {bbox.key()}")

    # Read as float; mosaic stores positive-down depth, NaN = land / dry / nodata.
    data = ds.read(1, window=window).astype(np.float32)
    # North-up GeoTIFF: row 0 is north. Flip so DepthField lats ascend south→north.
    data = np.flipud(data)
    # NaN / non-finite / non-positive → 0 (blocks when min_depth_m > 0).
    depth = np.where(np.isfinite(data) & (data > 0), data, 0.0).astype(np.float32)

    nrows, ncols = depth.shape
    lats = np.linspace(south, north, nrows, dtype=np.float64)
    lons = np.linspace(west, east, ncols, dtype=np.float64)

    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(npz_path, lats=lats, lons=lons, depth_m=depth)
    logger.info(
        "depth path=tif key=%s ms=%.0f shape=%sx%s",
        key,
        (time.perf_counter() - t0) * 1000,
        nrows,
        ncols,
    )
    return DepthField(lats, lons, depth)


def ensure_geo_context(course: Course, margin_deg: float | None = None) -> GeoContext:
    """Build GeoContext for a course using the same margin as prepare/route wind."""
    import time  # noqa: PLC0415

    from . import COURSE_WIND_MARGIN_DEG  # noqa: PLC0415

    margin = COURSE_WIND_MARGIN_DEG if margin_deg is None else margin_deg
    t0 = time.perf_counter()
    bbox = course_bbox(course, margin_deg=margin)
    land = fetch_land_for_bbox(bbox)
    depth = fetch_depth_for_bbox(bbox, land)
    logger.info(
        "geo context key=%s margin=%.1f total_ms=%.0f",
        bbox.key(),
        margin,
        (time.perf_counter() - t0) * 1000,
    )
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
    for npz_path in GEO_CACHE_DIR.glob("*_depth*.npz"):
        stem = npz_path.stem
        for suffix in ("_depth_chart", "_depth"):
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
    try:
        import rasterio.windows  # noqa: PLC0415

        ds = _open_region_depth()
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
    except ChartDataUnavailable:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("region_depth.tif sample failed: %s", exc)
        return None


def _in_region_depth_bounds(lat: float, lon: float) -> bool:
    """True when (lat, lon) lies inside the chart bathymetry mosaic footprint."""
    try:
        ds = _open_region_depth()
        left, bottom, right, top = ds.bounds
        return left <= lon <= right and bottom <= lat <= top
    except Exception:  # noqa: BLE001
        return False


def depth_at_point(lat: float, lon: float) -> float | None:
    """Water depth in metres — chart mosaic only (single source)."""
    mosaic = _sample_region_depth_tif(lat, lon)
    if mosaic is not None:
        return mosaic
    # Inside the chart mosaic: don't fall back to stale heuristic NPZs.
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
