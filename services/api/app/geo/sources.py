"""Fetch land (GSHHG) and bathymetry (GEBCO/EMODnet) for a bounding box."""

from __future__ import annotations

import io
import json
import logging

import httpx
import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..config import DATA_DIR, EMODNET_WCS_URL, GEBCO_WCS_URL, GSHHG_LAND_SHP, NATURAL_EARTH_LAND
from ..schemas import BBox

logger = logging.getLogger(__name__)

_DEPTH_GRID_N = 64

_NATURAL_EARTH_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_land.geojson"
)

_gshhg_tree: STRtree | None = None
_gshhg_polys: list[Polygon] | None = None


def _polygons_from_geojson_gj(gj: dict) -> list[Polygon]:
    polys: list[Polygon] = []
    for f in gj.get("features", []):
        geom = shape(f["geometry"])
        if isinstance(geom, Polygon):
            polys.append(geom)
        elif isinstance(geom, MultiPolygon):
            polys.extend(list(geom.geoms))
    return polys


def _load_natural_earth_land() -> list[Polygon]:
    """Global low-res land fallback when GSHHG is not installed."""
    if not NATURAL_EARTH_LAND.exists():
        try:
            logger.info("Downloading Natural Earth 110m land fallback…")
            resp = httpx.get(_NATURAL_EARTH_URL, timeout=60.0, follow_redirects=True)
            resp.raise_for_status()
            NATURAL_EARTH_LAND.parent.mkdir(parents=True, exist_ok=True)
            NATURAL_EARTH_LAND.write_bytes(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Natural Earth land download failed: %s", exc)
            return []
    return _polygons_from_geojson_gj(json.loads(NATURAL_EARTH_LAND.read_text()))


def _fallback_land(bbox: BBox) -> list[Polygon]:
    """Global land when GSHHG shapefile is not installed."""
    polys = _load_natural_earth_land()
    if not polys:
        sample = DATA_DIR / "masks" / "land_aegean.geojson"
        if sample.exists():
            polys = _polygons_from_geojson_gj(json.loads(sample.read_text()))
    if not polys:
        return []
    clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    selected: list[Polygon] = []
    for p in polys:
        if not p.intersects(clip):
            continue
        inter = p.intersection(clip)
        if isinstance(inter, Polygon) and not inter.is_empty:
            selected.append(inter)
        elif isinstance(inter, MultiPolygon):
            selected.extend(list(inter.geoms))
    return selected


def _shape_to_polygons(shape_obj) -> list[Polygon]:
    """Convert a pyshp shape to one or more shapely polygons."""
    import shapefile  # noqa: PLC0415

    if shape_obj.shapeType not in (
        shapefile.POLYGON,
        shapefile.POLYGONZ,
        shapefile.POLYGONM,
    ):
        return []

    parts = list(shape_obj.parts) + [len(shape_obj.points)]
    polys: list[Polygon] = []
    for i in range(len(parts) - 1):
        pts = shape_obj.points[parts[i] : parts[i + 1]]
        if len(pts) < 4:
            continue
        ring = [(float(x), float(y)) for x, y in pts]
        try:
            polys.append(Polygon(ring))
        except Exception:  # noqa: BLE001
            continue
    return polys


def _load_gshhg_index() -> None:
    global _gshhg_tree, _gshhg_polys
    if _gshhg_tree is not None:
        return

    if not GSHHG_LAND_SHP.exists():
        logger.warning(
            "GSHHG shapefile missing at %s — run services/api/scripts/fetch_gshhg.py",
            GSHHG_LAND_SHP,
        )
        _gshhg_polys = []
        _gshhg_tree = STRtree([])
        return

    import shapefile  # noqa: PLC0415

    logger.info("Loading GSHHG land index from %s", GSHHG_LAND_SHP)
    reader = shapefile.Reader(str(GSHHG_LAND_SHP))
    polys: list[Polygon] = []
    for rec in reader.iterShapeRecords():
        shp = rec.shape
        sb = shp.bbox
        if sb[0] > 180 or sb[1] > 90 or sb[2] < -180 or sb[3] < -90:
            continue
        for poly in _shape_to_polygons(shp):
            if poly.is_valid and not poly.is_empty:
                polys.append(poly)
            else:
                fixed = poly.buffer(0)
                if isinstance(fixed, Polygon) and not fixed.is_empty:
                    polys.append(fixed)
                elif isinstance(fixed, MultiPolygon):
                    polys.extend(g for g in fixed.geoms if not g.is_empty)

    _gshhg_polys = polys
    _gshhg_tree = STRtree(polys)
    logger.info("GSHHG index ready: %d land polygons", len(polys))


class GshhgMissingError(RuntimeError):
    """Raised when the chart baseline requires GSHHG but it is not installed."""


def require_gshhg() -> None:
    """Fail fast if high-res coastline shapefile is absent."""
    if not GSHHG_LAND_SHP.exists():
        raise GshhgMissingError(
            f"GSHHG coastline missing at {GSHHG_LAND_SHP}. "
            "Run: python services/api/scripts/fetch_gshhg.py"
        )


def gshhg_land(bbox: BBox, *, allow_fallback: bool = True) -> list[Polygon]:
    """Return land polygons clipped to bbox from bundled GSHHG high-res coastlines.

    When ``allow_fallback`` is False (chart baseline), raise if GSHHG is missing
    instead of silently substituting low-res Natural Earth.
    """
    _load_gshhg_index()
    assert _gshhg_tree is not None
    assert _gshhg_polys is not None

    if not _gshhg_polys:
        if not allow_fallback:
            require_gshhg()
            raise GshhgMissingError("GSHHG index empty")
        return _fallback_land(bbox)

    clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    clipped: list[Polygon] = []
    hits = _gshhg_tree.query(clip)
    for hit in hits:
        geom = _gshhg_polys[int(hit)] if isinstance(hit, (int, np.integer)) else hit
        if not geom.intersects(clip):
            continue
        inter = geom.intersection(clip)
        if isinstance(inter, Polygon) and not inter.is_empty:
            clipped.append(inter)
        elif isinstance(inter, MultiPolygon):
            clipped.extend(g for g in inter.geoms if not g.is_empty)

    if not clipped:
        logger.debug("GSHHG returned no land in bbox %s", bbox.key())
    return clipped


def _read_geotiff_depth(data: bytes) -> tuple[np.ndarray, tuple[float, float, float, float] | None]:
    """Minimal GeoTIFF reader for single-band float grids (WCS output).

    Returns (array, bounds) where bounds is (west, south, east, north) or None.
    """
    try:
        import rasterio  # noqa: PLC0415

        with rasterio.open(io.BytesIO(data)) as ds:
            arr = ds.read(1).astype(float)
            nodata = ds.nodata
            if nodata is not None:
                arr[arr == nodata] = np.nan
            b = ds.bounds
            return arr, (b.left, b.bottom, b.right, b.top)
    except ImportError:
        pass

    try:
        import tifffile  # noqa: PLC0415

        arr = tifffile.imread(io.BytesIO(data))
        return np.asarray(arr, dtype=float), None
    except ImportError:
        pass

    raise RuntimeError("Need rasterio or tifffile to decode bathymetry GeoTIFF")


def _fetch_emodnet_depth(bbox: BBox) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """EMODnet mean depth grid for European waters."""
    subset_lon = f"Long({bbox.lon_min},{bbox.lon_max})"
    subset_lat = f"Lat({bbox.lat_min},{bbox.lat_max})"
    url = (
        f"{EMODNET_WCS_URL}?service=WCS&version=2.0.1&request=GetCoverage"
        f"&coverageId=emodnet:mean&format=image/tiff"
        f"&subset={subset_lon}&subset={subset_lat}"
    )
    try:
        resp = httpx.get(url, timeout=120.0)
        resp.raise_for_status()
        arr, bounds = _read_geotiff_depth(resp.content)
        arr = np.where(np.isfinite(arr), np.abs(arr), 0.0)
        return _grid_from_raster(arr, bounds, bbox)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EMODnet WCS failed: %s", exc)
        return None


def _fetch_gebco_depth(bbox: BBox) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """GEBCO WCS depth grid (global)."""
    url = (
        f"{GEBCO_WCS_URL}?service=WCS&version=2.0.1&request=GetCoverage"
        f"&coverageId=gebco_latest&format=image/tiff"
        f"&subset=Long({bbox.lon_min},{bbox.lon_max})"
        f"&subset=Lat({bbox.lat_min},{bbox.lat_max})"
    )
    try:
        resp = httpx.get(url, timeout=120.0)
        resp.raise_for_status()
        arr, bounds = _read_geotiff_depth(resp.content)
        depth = np.where(arr < 0, -arr, 0.0)
        return _grid_from_raster(depth, bounds, bbox)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GEBCO WCS failed: %s", exc)
        return None


def _grid_from_raster(
    arr: np.ndarray,
    bounds: tuple[float, float, float, float] | None,
    bbox: BBox,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map a north-up raster to ascending lat/lon coordinate axes.

    GeoTIFF / WCS coverages are north-up: row 0 is the northern edge.
    We store ascending latitudes, so the array is flipped vertically when
    bounds indicate a north-up raster.
    """
    nrows, ncols = arr.shape
    if bounds is not None:
        west, south, east, north = bounds
        # North-up raster: flip so index 0 matches southern latitude.
        if north > south:
            arr = np.flipud(arr)
        lats = np.linspace(min(south, north), max(south, north), nrows)
        lons = np.linspace(west, east, ncols)
    else:
        lats = np.linspace(bbox.lat_min, bbox.lat_max, nrows)
        lons = np.linspace(bbox.lon_min, bbox.lon_max, ncols)
        # If caller passed descending lat linspace, flip to ascending.
        if lats[0] > lats[-1]:
            arr = np.flipud(arr)
            lats = lats[::-1].copy()
    return lats, lons, arr


def _depth_heuristic(land_polys: list[Polygon], bbox: BBox) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return _depth_heuristic_n(land_polys, bbox, _DEPTH_GRID_N)


def _depth_heuristic_n(
    land_polys: list[Polygon], bbox: BBox, n: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lats = np.linspace(bbox.lat_min, bbox.lat_max, n)
    lons = np.linspace(bbox.lon_min, bbox.lon_max, n)
    depth = np.zeros((lats.size, lons.size), dtype=float)
    land_union = unary_union(land_polys) if land_polys else None
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            pt = Point(float(lo), float(la))
            if land_union is not None and land_union.covers(pt):
                depth[i, j] = 0.0
            elif land_union is None:
                depth[i, j] = 500.0
            else:
                d_deg = land_union.boundary.distance(pt)
                depth[i, j] = max(1.0, min(500.0, d_deg * 111_000.0 * 0.5))
    return lats, lons, depth


def fetch_depth_grid(
    bbox: BBox, land_polys: list[Polygon]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lats, lons, depth_m) positive-down grid."""
    grid = _fetch_emodnet_depth(bbox)
    if grid is None:
        grid = _fetch_gebco_depth(bbox)
    if grid is not None:
        return grid
    return _depth_heuristic(land_polys, bbox)

