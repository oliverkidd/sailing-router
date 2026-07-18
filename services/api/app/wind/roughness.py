"""Coarse sea/land roughness for the WindNinja terrain bake.

Consumes the SAME single-source chart artifacts as routing:
  - land: data/geo/osm/land_chart.geojson (OSM behind coastline.pmtiles)
  - depth: data/geo/bathymetry/region_depth.tif

Does not fetch or recompute land/depth — only samples / rasterises for z0.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from ..config import DATA_DIR
from ..schemas import BBox

logger = logging.getLogger(__name__)

_LAND_CHART = DATA_DIR / "geo" / "osm" / "land_chart.geojson"
_REGION_DEPTH = DATA_DIR / "geo" / "bathymetry" / "region_depth.tif"

# Typical roughness lengths (m).
Z0_SEA = 0.0002
Z0_LAND = 0.03  # open terrain / grass — WindNinja vegetation fills finer detail
_WIND_HEIGHT_M = 10.0


def roughness_speedup_factor(z0: np.ndarray, *, z_m: float = _WIND_HEIGHT_M) -> np.ndarray:
    """Log-law speed ratio relative to open-sea z0 (land < 1, sea ≈ 1).

    u(z)/u_sea(z) ≈ ln(z/z0) / ln(z/z0_sea). Applied on top of WindNinja
    orographic speedup so coastal roughness contrast is present in the library.
    """
    z0_safe = np.maximum(np.asarray(z0, dtype=np.float64), 1e-6)
    denom = math.log(z_m / Z0_SEA)
    if abs(denom) < 1e-9:
        return np.ones(np.shape(z0), dtype=np.float32)
    ratio = np.log(z_m / z0_safe) / denom
    return np.clip(ratio, 0.05, 2.0).astype(np.float32)


def roughness_grid(
    bbox: BBox,
    lats: np.ndarray,
    lons: np.ndarray,
) -> np.ndarray:
    """Return z0 (m) on (nlat, nlon) from land mask + depth mosaic.

    Land cells → Z0_LAND; water → Z0_SEA. Missing land source → all Z0_SEA
    with a warning (bake still runs; WindNinja uses DEM for orography).
    """
    nlat, nlon = lats.size, lons.size
    z0 = np.full((nlat, nlon), Z0_SEA, dtype=np.float32)

    land = _land_mask_raster(bbox, lats, lons)
    if land is not None:
        z0[land] = Z0_LAND
        return z0

    # Fallback: treat NaN / non-positive depth as land-ish.
    depth = _depth_raster(bbox, lats, lons)
    if depth is not None:
        landish = ~np.isfinite(depth) | (depth <= 0)
        z0[landish] = Z0_LAND
    else:
        logger.warning(
            "roughness_grid: no land_chart.geojson or region_depth.tif — using sea z0"
        )
    return z0


def _land_mask_raster(
    bbox: BBox, lats: np.ndarray, lons: np.ndarray
) -> np.ndarray | None:
    if not _LAND_CHART.exists():
        return None
    try:
        import json  # noqa: PLC0415

        from rasterio import features as rio_features  # noqa: PLC0415
        from rasterio.transform import from_bounds  # noqa: PLC0415
        from shapely.geometry import box, mapping, shape  # noqa: PLC0415

        gj = json.loads(_LAND_CHART.read_text())
        clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
        shapes = []
        for feat in gj.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
                if not g.intersects(clip):
                    continue
                inter = g.intersection(clip)
                if inter.is_empty:
                    continue
                shapes.append((mapping(inter), 1))
            except Exception:  # noqa: BLE001
                continue
        if not shapes:
            return None

        nlat, nlon = lats.size, lons.size
        transform = from_bounds(
            bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max, nlon, nlat
        )
        # north-up mask from rasterize
        mask_n = rio_features.rasterize(
            shapes,
            out_shape=(nlat, nlon),
            transform=transform,
            fill=0,
            dtype="uint8",
            all_touched=False,
        )
        if lats.size >= 2 and lats[0] < lats[-1]:
            mask_n = mask_n[::-1, :]
        return mask_n.astype(bool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("land mask raster failed: %s", exc)
        return None


def _depth_raster(
    bbox: BBox, lats: np.ndarray, lons: np.ndarray
) -> np.ndarray | None:
    if not _REGION_DEPTH.exists():
        return None
    try:
        import rasterio  # noqa: PLC0415
        from rasterio.transform import from_bounds  # noqa: PLC0415
        from rasterio.warp import Resampling, reproject  # noqa: PLC0415

        nlat, nlon = lats.size, lons.size
        dst = np.full((nlat, nlon), np.nan, dtype=np.float32)
        transform = from_bounds(
            bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max, nlon, nlat
        )
        # from_bounds assumes north-up with row 0 at north — flip lats if ascending south→north.
        with rasterio.open(_REGION_DEPTH) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
                dst_nodata=np.nan,
            )
        # Rasterio north-up → flip to match ascending lats (south→north).
        if lats.size >= 2 and lats[0] < lats[-1]:
            dst = dst[::-1, :]
        return dst
    except Exception as exc:  # noqa: BLE001
        logger.warning("depth raster failed: %s", exc)
        return None


def chart_sources_present() -> dict[str, bool]:
    return {
        "land_chart": _LAND_CHART.exists(),
        "region_depth": _REGION_DEPTH.exists(),
        "dem_dir": (DATA_DIR / "dem").exists(),
    }
