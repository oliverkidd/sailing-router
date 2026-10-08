#!/usr/bin/env python3
"""Build MapLibre Terrain-RGB + baked hillshade XYZ tiles from Copernicus DEM.

Keeps elevation in metres through warp / upsample / OSM land clip / water
flatten, then encodes Mapbox Terrain-RGB PNGs under
apps/web/public/geo/terrain/{z}/{x}/{y}.png and a transparent-sea hillshade
under apps/web/public/geo/terrain/shade/{z}/{x}/{y}.png.

Requires: rasterio, numpy, scipy, gdalwarp, gdaldem, gdal2tiles.py, ogr2ogr.

Usage:
  python scripts/build_terrain_rgb.py
      Bake every 1° GLO-30 COG that intersects the full chart (REGION_BBOX).
  python scripts/build_terrain_rgb.py --cyclades
  python scripts/build_terrain_rgb.py --shade-only
  python scripts/build_terrain_rgb.py --normalize-only
  python scripts/build_terrain_rgb.py --sample
  python scripts/build_terrain_rgb.py --bbox lon_min,lat_min,lon_max,lat_max
  python scripts/build_terrain_rgb.py --force
"""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import (  # noqa: E402
    REGION_BBOX,
    chart_bbox,
    parse_bbox_arg,
    use_cyclades,
    use_sample,
)
from coastal_band import (  # noqa: E402
    bbox_near_coast,
    coastal_keep_on_grid,
    land_on_grid,
    lonlat_in_coastal_keep,
)

DEM_DIR = ROOT / "data" / "dem"
WORK = ROOT / "data" / "cache" / "tiles_build" / "terrain_rgb"
OUT_DIR = ROOT / "apps" / "web" / "public" / "geo" / "terrain"
SHADE_OUT_DIR = OUT_DIR / "shade"
COVERAGE_OUT = ROOT / "apps" / "web" / "public" / "geo" / "dem_coverage.geojson"
META_OUT = OUT_DIR / "tilejson.json"
SHADE_META_OUT = SHADE_OUT_DIR / "tilejson.json"
LAND_GEOJSONL = ROOT / "data" / "cache" / "tiles_build" / "land.geojsonl"
OSM_WATER = ROOT / "data" / "cache" / "tiles_build" / "osm_water.geojsonl"

# MapLibre setTerrain is gated to fine zoom; shade raster is useful from mid zoom.
_MINZOOM = 8
_MAXZOOM = 16
_UPSAMPLE = 2.0
_LAND_FEATHER_PX = 2.0
_NODATA = -9999.0
# Matches MapView LAND_FILL_COLOR (#c8c8c8); gdaldem flat-ground hillshade ~180.
_LAND_GREY = 200.0
_HILLSHADE_BLUR_SIGMA = 1.05
# Halfway between the original subtle bake (z=1, 55% land) and the crushed one.
_HILLSHADE_Z = "1.7"
_HILLSHADE_CONTRAST = 1.32
_SHADE_LAND_MIX = 0.42
# Keep lee slopes readable grey (not black) and ridges light grey (not white).
_SHADE_GREY_MIN = 108.0
_SHADE_GREY_MAX = 216.0
_TILEJSON_VERSION = "3.1.0"
# Mapbox Terrain-RGB for 0 m — fill gdal2tiles empty/alpha padding.
_ZERO_M_RGB = (1, 134, 160)


def _find_dem(bb: dict[str, float]) -> Path:
    """Prefer a region mosaic that covers the bbox centre, else any mosaic / tile."""
    mosaics = sorted(DEM_DIR.glob("region_dem*.tif")) + sorted(DEM_DIR.glob("region_dem*.tiff"))
    if not mosaics and not DEM_DIR.exists():
        raise SystemExit(f"[terrain-rgb] no DEM dir at {DEM_DIR} — run fetch_dem.py first")

    clat = (bb["lat_min"] + bb["lat_max"]) / 2
    clon = (bb["lon_min"] + bb["lon_max"]) / 2

    try:
        import rasterio  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[terrain-rgb] rasterio required: {exc}") from exc

    for path in mosaics:
        try:
            with rasterio.open(path) as ds:
                b = ds.bounds
                if b.left <= clon <= b.right and b.bottom <= clat <= b.top:
                    return path
        except Exception:  # noqa: BLE001
            continue

    if use_cyclades():
        preferred = DEM_DIR / "region_dem_cyclades.tif"
        if preferred.exists():
            return preferred
    if use_sample():
        preferred = DEM_DIR / "region_dem_sample.tif"
        if preferred.exists():
            return preferred
    if mosaics:
        return mosaics[0]

    tiles = sorted(DEM_DIR.glob("*.tif")) + sorted(DEM_DIR.glob("*.tiff"))
    if not tiles:
        raise SystemExit("[terrain-rgb] no GeoTIFF in data/dem/")
    return tiles[0]


def _bbox_of_dem(path: Path) -> dict[str, float]:
    import rasterio  # noqa: PLC0415

    with rasterio.open(path) as ds:
        b = ds.bounds
        return {
            "lon_min": float(b.left),
            "lat_min": float(b.bottom),
            "lon_max": float(b.right),
            "lat_max": float(b.top),
        }


_COG_RE = re.compile(
    r"Copernicus_DSM_COG_10_([NS])(\d+)_00_([EW])(\d+)_00_DEM",
    re.I,
)
_DONE_DIR = WORK / "done"
_REGION_STAMP = WORK / ".region_complete"


def _bbox_span_deg2(bb: dict[str, float]) -> float:
    return max(0.0, bb["lon_max"] - bb["lon_min"]) * max(0.0, bb["lat_max"] - bb["lat_min"])


def _bboxes_intersect(a: dict[str, float], b: dict[str, float]) -> bool:
    return not (
        a["lon_max"] <= b["lon_min"]
        or a["lon_min"] >= b["lon_max"]
        or a["lat_max"] <= b["lat_min"]
        or a["lat_min"] >= b["lat_max"]
    )


def _same_bbox(a: dict[str, float], b: dict[str, float], eps: float = 0.05) -> bool:
    return all(abs(float(a[k]) - float(b[k])) < eps for k in ("lon_min", "lat_min", "lon_max", "lat_max"))


def _cog_sw(path: Path) -> tuple[int, int] | None:
    m = _COG_RE.search(path.stem)
    if not m:
        return None
    lat = int(m.group(2))
    lon = int(m.group(4))
    if m.group(1).upper() == "S":
        lat = -lat
    if m.group(3).upper() == "W":
        lon = -lon
    return lat, lon


def _cog_bbox(path: Path) -> dict[str, float] | None:
    sw = _cog_sw(path)
    if sw is None:
        return None
    lat, lon = sw
    return {
        "lon_min": float(lon),
        "lat_min": float(lat),
        "lon_max": float(lon + 1),
        "lat_max": float(lat + 1),
    }


def _one_degree_cogs(bb: dict[str, float]) -> list[Path]:
    if not DEM_DIR.exists():
        return []
    out: list[Path] = []
    for path in sorted(DEM_DIR.glob("Copernicus_DSM_*.tif")):
        if path.stat().st_size < 100_000:
            continue
        tile_bb = _cog_bbox(path)
        if tile_bb is None:
            continue
        if _bboxes_intersect(tile_bb, bb):
            out.append(path)
    return out


def _done_path(stem: str) -> Path:
    return _DONE_DIR / f"{stem}.done"


def _is_done(stem: str) -> bool:
    return _done_path(stem).exists()


def _mark_done(stem: str, note: str) -> None:
    _DONE_DIR.mkdir(parents=True, exist_ok=True)
    _done_path(stem).write_text(f"{note}\n", encoding="utf-8")


def _stamp_prebaked_from_tilejson() -> int:
    """Mark 1° COGs already in the published XYZ tree so a full-chart run skips them."""
    if not META_OUT.exists():
        return 0
    try:
        meta = json.loads(META_OUT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    bounds = meta.get("bounds")
    if not isinstance(bounds, list) or len(bounds) < 4:
        return 0
    covered = {
        "lon_min": float(bounds[0]),
        "lat_min": float(bounds[1]),
        "lon_max": float(bounds[2]),
        "lat_max": float(bounds[3]),
    }
    if _bbox_span_deg2(covered) > 8.0:
        return 0
    n = 0
    for path in _one_degree_cogs(covered):
        if not _is_done(path.stem):
            _mark_done(path.stem, "prebaked")
            n += 1
    if n:
        print(f"[terrain-rgb] stamped {n} already-published 1° tile(s)", flush=True)
    return n


def _cog_has_orography(path: Path) -> bool:
    import rasterio  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415

    try:
        with rasterio.open(path) as ds:
            h = min(ds.height, 128)
            w = min(ds.width, 128)
            data = ds.read(1, out_shape=(h, w), resampling=Resampling.bilinear)
            nodata = ds.nodata
        arr = np.asarray(data, dtype=np.float32)
        mask = ~np.isfinite(arr) | (arr <= -1000.0)
        if nodata is not None:
            mask |= arr == nodata
        valid = arr[~mask]
        if valid.size < 40:
            return False
        return float(np.nanmax(valid)) >= 1.5
    except Exception:  # noqa: BLE001
        return True


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = Path(base) / name
        if p.exists():
            return str(p)
    return None


def _run(cmd: list[str]) -> None:
    print("[terrain-rgb]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _encode_terrain_rgb(elev: np.ndarray, nodata_mask: np.ndarray) -> np.ndarray:
    """Mapbox Terrain-RGB: height = -10000 + ((R*256*256 + G*256 + B) * 0.1)."""
    h = elev.astype(np.float64)
    h = np.where(nodata_mask, 0.0, h)  # sea / nodata → 0 m for continuous mesh
    v = np.clip((h + 10000.0) * 10.0, 0.0, 256.0**3 - 1.0)
    r = np.floor(v / 65536.0).astype(np.uint8)
    g = np.floor(np.mod(v, 65536.0) / 256.0).astype(np.uint8)
    b = np.floor(np.mod(v, 256.0)).astype(np.uint8)
    return np.stack([r, g, b], axis=0)


def _clip_float_wgs(src_path: Path, bb: dict[str, float], dest: Path) -> None:
    """Clip DEM to bbox as float32 WGS84 GeoTIFF (elevation metres, not RGB)."""
    import rasterio  # noqa: PLC0415
    from rasterio.windows import from_bounds  # noqa: PLC0415
    from rasterio.windows import transform as window_transform  # noqa: PLC0415

    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        win = from_bounds(
            bb["lon_min"],
            bb["lat_min"],
            bb["lon_max"],
            bb["lat_max"],
            transform=src.transform,
        )
        win = win.round_offsets().round_lengths()
        if win.width < 2 or win.height < 2:
            raise SystemExit("[terrain-rgb] DEM window empty for bbox — check coverage")
        data = src.read(1, window=win).astype(np.float32)
        transform = window_transform(win, src.transform)
        nodata = src.nodata
        mask = ~np.isfinite(data)
        if nodata is not None:
            mask |= data == nodata
        mask |= data <= -1000.0
        data = np.where(mask, _NODATA, data)
        profile = src.profile.copy()
        profile.update(
            {
                "driver": "GTiff",
                "count": 1,
                "dtype": "float32",
                "height": data.shape[0],
                "width": data.shape[1],
                "transform": transform,
                "nodata": _NODATA,
                "compress": "deflate",
            }
        )
        profile.pop("photometric", None)
        with rasterio.open(dest, "w", **profile) as dst:
            dst.write(data, 1)


def _warp_webmerc_float(src: Path, dest: Path, *, upsample: float) -> None:
    """Reproject float metres to EPSG:3857 with cubicspline, then upsample."""
    gdalwarp = _which("gdalwarp")
    if not gdalwarp:
        raise SystemExit("[terrain-rgb] gdalwarp not found on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    native = dest.with_name(dest.stem + "_native" + dest.suffix)
    _run(
        [
            gdalwarp,
            "-t_srs",
            "EPSG:3857",
            "-r",
            "cubicspline",
            "-of",
            "GTiff",
            "-dstnodata",
            str(_NODATA),
            "-co",
            "COMPRESS=DEFLATE",
            "-overwrite",
            str(src),
            str(native),
        ]
    )
    import rasterio  # noqa: PLC0415

    with rasterio.open(native) as ds:
        px = abs(ds.transform.a)
        py = abs(ds.transform.e)
    if upsample <= 1.0:
        shutil.copy2(native, dest)
        return
    _run(
        [
            gdalwarp,
            "-r",
            "cubicspline",
            "-tr",
            str(px / upsample),
            str(py / upsample),
            "-of",
            "GTiff",
            "-dstnodata",
            str(_NODATA),
            "-co",
            "COMPRESS=DEFLATE",
            "-overwrite",
            str(native),
            str(dest),
        ]
    )


def _clip_tag(bb: dict[str, float]) -> str:
    return (
        f"{bb['lon_min']:.4f}_{bb['lat_min']:.4f}_"
        f"{bb['lon_max']:.4f}_{bb['lat_max']:.4f}"
    )


def _clip_polygons_wgs(src: Path, dest: Path, bb: dict[str, float]) -> int:
    """Clip WGS84 GeoJSONSeq to bbox. GeoJSONSeq cannot store EPSG:3857."""
    ogr2ogr = _which("ogr2ogr")
    if not ogr2ogr:
        raise SystemExit("[terrain-rgb] ogr2ogr not found on PATH")
    if dest.exists() and dest.stat().st_size > 0:
        n = 0
        with dest.open() as fh:
            for line in fh:
                if line.strip():
                    n += 1
        if n > 0:
            print(f"[terrain-rgb] reuse clip {dest.name} ({n} features)", flush=True)
            return n
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ogr2ogr,
            "-f",
            "GeoJSONSeq",
            "-clipsrc",
            str(bb["lon_min"]),
            str(bb["lat_min"]),
            str(bb["lon_max"]),
            str(bb["lat_max"]),
            "-nlt",
            "PROMOTE_TO_MULTI",
            str(dest),
            str(src),
        ]
    )
    n = 0
    with dest.open() as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


def _iter_geoms(path: Path):
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            geom = feat.get("geometry")
            if geom:
                yield geom


def _project_geom_to_3857(geom: dict, transformer) -> dict | None:
    from shapely.geometry import mapping, shape  # noqa: PLC0415
    from shapely.ops import transform as shp_transform  # noqa: PLC0415

    def project(x, y, z=None):
        return transformer.transform(x, y)

    try:
        g = shp_transform(project, shape(geom))
        if g.is_empty:
            return None
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            return None
        return mapping(g)
    except Exception:  # noqa: BLE001
        return None


def _rasterize_mask(geoms_path: Path, shape: tuple[int, int], transform) -> np.ndarray:
    """Rasterize WGS84 GeoJSONSeq onto a Web Mercator grid."""
    from pyproj import Transformer  # noqa: PLC0415
    from rasterio.features import rasterize  # noqa: PLC0415

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    shapes = []
    for geom in _iter_geoms(geoms_path):
        projected = _project_geom_to_3857(geom, transformer)
        if projected is not None:
            shapes.append((projected, 1))
    if not shapes:
        return np.zeros(shape, dtype=np.uint8)
    return rasterize(
        shapes,
        out_shape=shape,
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=True,
    )


def _land_weight(land: np.ndarray, feather_px: float) -> np.ndarray:
    from scipy.ndimage import distance_transform_edt  # noqa: PLC0415

    dist = distance_transform_edt(land > 0)
    if feather_px <= 0:
        return (land > 0).astype(np.float32)
    return np.clip(dist / feather_px, 0.0, 1.0).astype(np.float32)


def _flatten_water(elev: np.ndarray, water: np.ndarray) -> np.ndarray:
    """Set each water body to the median elevation of its shoreline ring."""
    from scipy.ndimage import binary_erosion, label  # noqa: PLC0415

    out = elev.copy()
    labeled, n_bodies = label(water > 0)
    flattened = 0
    for i in range(1, n_bodies + 1):
        body = labeled == i
        n_px = int(body.sum())
        if n_px == 0:
            continue
        if n_px >= 4:
            ring = body & ~binary_erosion(body, iterations=1)
            if not np.any(ring):
                ring = body
        else:
            ring = body
        samples = elev[ring]
        samples = samples[np.isfinite(samples)]
        if samples.size == 0:
            continue
        out[body] = float(np.median(samples))
        flattened += 1
    print(f"[terrain-rgb] flattened {flattened} water bodies ({int((water > 0).sum())} px)", flush=True)
    return out


def _clean_elev(data: np.ndarray) -> np.ndarray:
    elev = data.astype(np.float32)
    bad = ~np.isfinite(elev) | (elev == _NODATA) | (elev <= -1000.0)
    elev = np.where(bad, 0.0, elev)
    return np.clip(elev, 0.0, 4000.0)


def _write_geotiff(
    path: Path,
    data: np.ndarray,
    *,
    profile: dict,
    count: int | None = None,
    colorinterp: list | None = None,
) -> None:
    import rasterio  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    if data.ndim == 2:
        bands = data[np.newaxis, ...]
    else:
        bands = data
    n = count if count is not None else bands.shape[0]
    prof = {
        "driver": "GTiff",
        "count": n,
        "dtype": bands.dtype,
        "height": bands.shape[1],
        "width": bands.shape[2],
        "crs": profile["crs"],
        "transform": profile["transform"],
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(bands)
        if colorinterp is not None:
            dst.colorinterp = colorinterp


def _process_mesh(
    merc_path: Path,
    bb: dict[str, float],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, object, dict, np.ndarray]:
    """Land-clip + water-flatten + 20 km coastal keep.

    Returns elev, land_weight, water, transform, profile, keep.
    """
    import rasterio  # noqa: PLC0415

    with rasterio.open(merc_path) as src:
        elev = _clean_elev(src.read(1))
        transform = src.transform
        profile = src.profile.copy()
        shape = (src.height, src.width)

    profile.update({"nodata": None, "dtype": "float32", "count": 1})
    profile.pop("photometric", None)

    weight = np.ones(shape, dtype=np.float32)
    if LAND_GEOJSONL.exists():
        land_clip = WORK / f"land_clip_{_clip_tag(bb)}.geojsonl"
        n_land = _clip_polygons_wgs(LAND_GEOJSONL, land_clip, bb)
        print(f"[terrain-rgb] OSM land features={n_land}", flush=True)
        if n_land > 0:
            land = _rasterize_mask(land_clip, shape, transform)
            n_land_px = int((land > 0).sum())
            print(f"[terrain-rgb] land px={n_land_px} / {land.size}", flush=True)
            if n_land_px > 0:
                weight = _land_weight(land, _LAND_FEATHER_PX)
            else:
                print("[terrain-rgb] land raster empty — keeping DEM shoreline", flush=True)
        else:
            print("[terrain-rgb] OSM land clip empty — keeping DEM shoreline", flush=True)
    else:
        print(f"[terrain-rgb] skip land clip — missing {LAND_GEOJSONL}", flush=True)
        land_grid = land_on_grid(shape, transform, profile.get("crs") or "EPSG:3857")
        if land_grid is not None and float(land_grid.max()) > 0:
            weight = _land_weight(land_grid, _LAND_FEATHER_PX)
            print(
                f"[terrain-rgb] coastline land px={int((land_grid > 0).sum())} / {land_grid.size}",
                flush=True,
            )

    water = np.zeros(shape, dtype=np.uint8)
    water_flat = elev
    if OSM_WATER.exists():
        water_clip = WORK / f"water_clip_{_clip_tag(bb)}.geojsonl"
        n_water = _clip_polygons_wgs(OSM_WATER, water_clip, bb)
        print(f"[terrain-rgb] OSM water features={n_water}", flush=True)
        if n_water > 0:
            water = _rasterize_mask(water_clip, shape, transform)
            if np.any(water):
                water_flat = _flatten_water(elev, water)
            else:
                print("[terrain-rgb] water raster empty", flush=True)
        else:
            print("[terrain-rgb] no OSM water in bbox", flush=True)
    else:
        print(f"[terrain-rgb] skip water flatten — missing {OSM_WATER}", flush=True)

    # Sea → 0 via OSM land feather; restore flattened lakes (holes in land).
    out = water_flat * weight
    if np.any(water):
        out = np.where(water > 0, water_flat, out)

    keep = coastal_keep_on_grid(shape, transform, profile.get("crs") or "EPSG:3857")
    n_keep = int(keep.sum())
    print(f"[terrain-rgb] coastal strip px={n_keep} / {keep.size}", flush=True)
    weight = weight * keep
    if np.any(water):
        water = np.where(keep > 0.5, water, 0).astype(np.uint8)
    out = out * keep
    return out.astype(np.float32), weight, water, transform, profile, keep


def _crop_to_mask(
    arrays: list[np.ndarray],
    mask: np.ndarray,
    profile: dict,
    *,
    pad: int = 16,
) -> tuple[list[np.ndarray], dict]:
    """Crop 2d arrays to the keep bbox so gdal2tiles does not walk empty land."""
    from rasterio.windows import Window  # noqa: PLC0415
    from rasterio.windows import transform as window_transform  # noqa: PLC0415

    rows = np.any(mask >= 0.5, axis=1)
    cols = np.any(mask >= 0.5, axis=0)
    if not np.any(rows) or not np.any(cols):
        return arrays, profile
    r_idx = np.flatnonzero(rows)
    c_idx = np.flatnonzero(cols)
    r0 = max(0, int(r_idx[0]) - pad)
    r1 = min(mask.shape[0], int(r_idx[-1]) + 1 + pad)
    c0 = max(0, int(c_idx[0]) - pad)
    c1 = min(mask.shape[1], int(c_idx[-1]) + 1 + pad)
    if r1 - r0 < 2 or c1 - c0 < 2:
        return arrays, profile
    prof = dict(profile)
    prof["transform"] = window_transform(Window(c0, r0, c1 - c0, r1 - r0), profile["transform"])
    prof["height"] = r1 - r0
    prof["width"] = c1 - c0
    cropped = [a[r0:r1, c0:c1] for a in arrays]
    print(
        f"[terrain-rgb] cropped mesh {mask.shape[0]}x{mask.shape[1]} → {r1 - r0}x{c1 - c0}",
        flush=True,
    )
    return cropped, prof


def _write_rgb_geotiff(
    elev: np.ndarray,
    profile: dict,
    dest: Path,
    omit: np.ndarray | None = None,
) -> None:
    from rasterio.enums import ColorInterp  # noqa: PLC0415

    rgb = _encode_terrain_rgb(elev, nodata_mask=~np.isfinite(elev))
    if omit is not None:
        rgb[:, omit] = 0
        alpha = np.where(omit, 0, 255).astype(np.uint8)
        rgba = np.concatenate([rgb, alpha[np.newaxis, ...]], axis=0)
        _write_geotiff(
            dest,
            rgba,
            profile=profile,
            count=4,
            colorinterp=[
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.alpha,
            ],
        )
        return
    _write_geotiff(
        dest,
        rgb,
        profile=profile,
        count=3,
        colorinterp=[ColorInterp.red, ColorInterp.green, ColorInterp.blue],
    )


def _gdaldem_hillshade(src: Path, dest: Path) -> None:
    gdaldem = _which("gdaldem")
    if not gdaldem:
        raise SystemExit("[terrain-rgb] gdaldem not found on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            gdaldem,
            "hillshade",
            "-combined",
            "-az",
            "315",
            "-alt",
            "45",
            "-z",
            _HILLSHADE_Z,
            "-compute_edges",
            "-of",
            "GTiff",
            str(src),
            str(dest),
        ]
    )


def _write_shade_rgba(
    hillshade_path: Path,
    land_weight: np.ndarray,
    profile: dict,
    dest: Path,
    water: np.ndarray | None = None,
) -> None:
    import rasterio  # noqa: PLC0415

    with rasterio.open(hillshade_path) as src:
        hs = src.read(1).astype(np.float32)
    from scipy.ndimage import gaussian_filter  # noqa: PLC0415

    hs = gaussian_filter(hs, sigma=_HILLSHADE_BLUR_SIGMA)
    hs = (hs - 180.0) * _HILLSHADE_CONTRAST + 180.0
    grey = np.clip(
        _SHADE_LAND_MIX * _LAND_GREY + (1.0 - _SHADE_LAND_MIX) * hs,
        _SHADE_GREY_MIN,
        _SHADE_GREY_MAX,
    ).astype(np.uint8)
    alpha_f = land_weight
    if water is not None and np.any(water):
        alpha_f = np.maximum(alpha_f, (water > 0).astype(np.float32))
    alpha = np.clip(alpha_f * 255.0, 0.0, 255.0).astype(np.uint8)
    from rasterio.enums import ColorInterp  # noqa: PLC0415

    rgba = np.stack([grey, grey, grey, alpha], axis=0)
    _write_geotiff(
        dest,
        rgba,
        profile=profile,
        count=4,
        colorinterp=[
            ColorInterp.red,
            ColorInterp.green,
            ColorInterp.blue,
            ColorInterp.alpha,
        ],
    )


def _gdal2tiles(
    src: Path,
    out_dir: Path,
    resampling: str,
    *,
    srcnodata: str | None = None,
) -> None:
    gdal2tiles = _which("gdal2tiles.py") or _which("gdal2tiles")
    if not gdal2tiles:
        raise SystemExit("[terrain-rgb] gdal2tiles.py not found on PATH")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        gdal2tiles,
        "-z",
        f"{_MINZOOM}-{_MAXZOOM}",
        "-r",
        resampling,
        "--tilesize=256",
        "--processes=4",
        "--xyz",
        "-x",
        "--legacy",
        "-w",
        "none",
    ]
    if srcnodata:
        cmd.extend(["-a", srcnodata])
    cmd.extend([str(src), str(out_dir)])
    _run(cmd)


def _iter_xyz_pngs(root: Path):
    if not root.exists():
        return
    for zdir in sorted(root.iterdir()):
        if not (zdir.is_dir() and zdir.name.isdigit()):
            continue
        for xdir in zdir.iterdir():
            if xdir.is_dir():
                yield from xdir.glob("*.png")


def _normalize_shade_png(path_str: str) -> None:
    """Keep land hillshade; never upload a mostly-transparent raster tile.

    MapLibre's raster shader fills fully- or mostly-transparent tiles magenta
    (the empty-texture colour). Sea / gdal2tiles nodata becomes white at alpha
    1/255 so the texture is valid but invisible over the chart.
    """
    from PIL import Image  # noqa: PLC0415

    path = Path(path_str)
    with Image.open(path) as im:
        px = np.array(im.convert("RGBA"))
    sea = (px[:, :, 3] == 0) | (
        (px[:, :, 0] <= 8) & (px[:, :, 1] <= 8) & (px[:, :, 2] <= 8)
    )
    # Flat light grey is empty relief (open water, or a slope crushed to one
    # colour). Real hillshade varies, so leave that tile's opaque pixels.
    opaque = px[:, :, 3] > 200
    if int(opaque.sum()) > 64:
        grey = px[:, :, 0][opaque].astype(np.float32)
        if float(grey.std()) < 2.0 and float(grey.mean()) > 200.0:
            sea = sea | opaque
    if not np.any(sea):
        return
    px[:, :, 0][sea] = 255
    px[:, :, 1][sea] = 255
    px[:, :, 2][sea] = 255
    px[:, :, 3][sea] = 1
    Image.fromarray(px, "RGBA").save(path, "PNG", compress_level=3)


def _normalize_dem_png(path_str: str) -> None:
    """Force 3-band Terrain-RGB; MapLibre raster-dem cannot decode PNG alpha.

    gdal2tiles writes src-nodata as 0,0,0 / alpha 0; those pixels must become
    Mapbox 0 m, not height −10000 m.
    """
    from PIL import Image  # noqa: PLC0415

    path = Path(path_str)
    with Image.open(path) as im:
        mode = im.mode
        px = np.array(im.convert("RGBA") if mode != "RGB" else im)
    changed = False
    if px.ndim == 3 and px.shape[2] == 4:
        rgb = px[:, :, :3].copy()
        rgb[px[:, :, 3] == 0] = _ZERO_M_RGB
        changed = True
    else:
        rgb = px[:, :, :3].copy() if px.ndim == 3 else px
    black = (rgb[:, :, 0] == 0) & (rgb[:, :, 1] == 0) & (rgb[:, :, 2] == 0)
    if np.any(black):
        rgb[black] = _ZERO_M_RGB
        changed = True
    if not changed:
        return
    Image.fromarray(rgb, "RGB").save(path, "PNG", compress_level=6)


def _lonlat_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _tile_center_lonlat(z: int, x: int, y: int) -> tuple[float, float]:
    n = 2 ** z
    lon = (x + 0.5) / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 0.5) / n)))
    return lon, math.degrees(lat_rad)


_SCRUB_MIN_ZOOM = 12


def _scrub_xyz_away_from_coast(
    root: Path, bb: dict[str, float], *, min_z: int = _SCRUB_MIN_ZOOM
) -> int:
    """Delete published high-zoom tiles whose centre is outside the coastal strip."""
    if not root.exists():
        return 0
    n = 0
    for z in range(min_z, _MAXZOOM + 1):
        x_nw, y_nw = _lonlat_to_tile(bb["lat_max"], bb["lon_min"], z)
        x_se, y_se = _lonlat_to_tile(bb["lat_min"], bb["lon_max"], z)
        nmax = 2 ** z
        x0 = max(0, min(x_nw, x_se))
        x1 = min(nmax - 1, max(x_nw, x_se))
        y0 = max(0, min(y_nw, y_se))
        y1 = min(nmax - 1, max(y_nw, y_se))
        for x in range(x0, x1 + 1):
            xdir = root / str(z) / str(x)
            if not xdir.is_dir():
                continue
            for y in range(y0, y1 + 1):
                dest = xdir / f"{y}.png"
                if not dest.exists():
                    continue
                lon, lat = _tile_center_lonlat(z, x, y)
                if lonlat_in_coastal_keep(lon, lat):
                    continue
                dest.unlink(missing_ok=True)
                n += 1
    if n:
        print(
            f"[terrain-rgb] scrubbed {n} inland pngs in {root} z>={min_z}",
            flush=True,
        )
    return n


def _empty_shade_png_bytes() -> bytes:
    from PIL import Image  # noqa: PLC0415

    # Alpha 1 so MapLibre does not treat the tile as an empty (magenta) texture.
    img = Image.new("RGBA", (256, 256), (255, 255, 255, 1))
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def _fill_missing_shade_tiles(root: Path, bounds: list[float] | tuple[float, ...]) -> None:
    """Write placeholder PNGs for every XYZ cell that intersects the DEM bbox.

    gdal2tiles only emits tiles that contain raster data. MapLibre still
    requests every tile overlapping `bounds`, and a 404 renders magenta.
    """
    lon_min, lat_min, lon_max, lat_max = (float(b) for b in bounds[:4])
    blob = _empty_shade_png_bytes()
    n = 0
    for z in range(_MINZOOM, _MAXZOOM + 1):
        x_nw, y_nw = _lonlat_to_tile(lat_max, lon_min, z)
        x_se, y_se = _lonlat_to_tile(lat_min, lon_max, z)
        nmax = 2 ** z
        x0 = max(0, min(x_nw, x_se) - 1)
        x1 = min(nmax - 1, max(x_nw, x_se) + 1)
        y0 = max(0, min(y_nw, y_se) - 1)
        y1 = min(nmax - 1, max(y_nw, y_se) + 1)
        for x in range(x0, x1 + 1):
            xdir = root / str(z) / str(x)
            for y in range(y0, y1 + 1):
                dest = xdir / f"{y}.png"
                if dest.exists():
                    continue
                xdir.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
                n += 1
    print(f"[terrain-rgb] filled {n} missing shade tiles in {root}", flush=True)


def _normalize_xyz(root: Path, kind: str) -> None:
    pngs = [str(p) for p in _iter_xyz_pngs(root)]
    if not pngs:
        print(f"[terrain-rgb] normalize {kind}: no pngs in {root}", flush=True)
        return
    fn = _normalize_shade_png if kind == "shade" else _normalize_dem_png
    print(f"[terrain-rgb] normalize {kind} {len(pngs)} tiles in {root}", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    n = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(fn, p) for p in pngs]
        for fut in as_completed(futs):
            fut.result()
            n += 1
            if n % 4000 == 0 or n == len(pngs):
                print(f"[terrain-rgb] normalize {kind} {n}/{len(pngs)}", flush=True)


def _publish_xyz(src: Path, dest: Path, *, merge: bool = False, skip_existing: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if not merge:
        for child in list(dest.iterdir()):
            if child.is_dir() and child.name.isdigit():
                shutil.rmtree(child)
    n = 0
    for zdir in src.iterdir():
        if not (zdir.is_dir() and zdir.name.isdigit()):
            continue
        dest_z = dest / zdir.name
        dest_z.mkdir(parents=True, exist_ok=True)
        for xdir in zdir.iterdir():
            if not xdir.is_dir():
                continue
            dest_x = dest_z / xdir.name
            dest_x.mkdir(parents=True, exist_ok=True)
            for png in xdir.glob("*.png"):
                dest_png = dest_x / png.name
                if skip_existing and dest_png.exists():
                    png.unlink(missing_ok=True)
                    continue
                try:
                    png.replace(dest_png)
                except OSError:
                    shutil.copy2(png, dest_png)
                    png.unlink(missing_ok=True)
                n += 1
    print(f"[terrain-rgb] published {n} pngs → {dest} (merge={merge})", flush=True)


def _scrub_gdal2tiles_junk(root: Path) -> None:
    if not root.exists():
        return
    for junk in root.glob("*.html"):
        junk.unlink(missing_ok=True)
    for name in ("tilemapresource.xml", "openlayers.html", "googlemaps.html"):
        p = root / name
        if p.exists():
            p.unlink()


def _wgs_bounds_of(path: Path) -> tuple[float, float, float, float]:
    import rasterio  # noqa: PLC0415
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    with rasterio.open(path) as ds:
        b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
    return (min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))


def _write_meta(
    bounds: tuple[float, float, float, float],
    dem_name: str,
    tile_origin: str = "",
) -> None:
    lon_min, lat_min, lon_max, lat_max = bounds
    if COVERAGE_OUT.exists():
        COVERAGE_OUT.unlink()
        print(f"[terrain-rgb] removed test overlay {COVERAGE_OUT}", flush=True)
    META_OUT.parent.mkdir(parents=True, exist_ok=True)
    META_OUT.write_text(
        json.dumps(
            {
                "tilejson": "3.0.0",
                "name": "MasterWind Terrain-RGB",
                "description": f"Mapbox Terrain-RGB from {dem_name}",
                "version": _TILEJSON_VERSION,
                "scheme": "xyz",
                "tiles": [f"{tile_origin}/geo/terrain/{{z}}/{{x}}/{{y}}.png"],
                "minzoom": _MINZOOM,
                "maxzoom": _MAXZOOM,
                "bounds": [lon_min, lat_min, lon_max, lat_max],
                "encoding": "mapbox",
                "tileSize": 256,
                "shade": True,
                "shadeTiles": [f"{tile_origin}/geo/terrain/shade/{{z}}/{{x}}/{{y}}.png"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    SHADE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHADE_META_OUT.write_text(
        json.dumps(
            {
                "tilejson": "3.0.0",
                "name": "MasterWind Terrain Hillshade",
                "description": f"Baked combined hillshade from {dem_name}",
                "version": _TILEJSON_VERSION,
                "scheme": "xyz",
                "tiles": [f"{tile_origin}/geo/terrain/shade/{{z}}/{{x}}/{{y}}.png"],
                "minzoom": _MINZOOM,
                "maxzoom": _MAXZOOM,
                "bounds": [lon_min, lat_min, lon_max, lat_max],
                "tileSize": 256,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[terrain-rgb] wrote {META_OUT}")
    print(f"[terrain-rgb] wrote {SHADE_META_OUT}")


def _bake_dem_bbox(
    dem: Path,
    bb: dict[str, float],
    *,
    shade_only: bool,
    merge: bool,
    fill_missing: bool,
    work: Path,
    skip_existing: bool = False,
) -> tuple[float, float, float, float]:
    work.mkdir(parents=True, exist_ok=True)
    float_wgs = work / "elev_wgs84.tif"
    float_merc = work / "elev_3857.tif"
    processed = work / "elev_processed.tif"
    rgb_merc = work / "terrain_rgb_3857.tif"
    hs_tif = work / "hillshade.tif"
    shade_rgba = work / "hillshade_rgba.tif"
    xyz_rgb = work / "xyz_rgb"
    xyz_shade = work / "xyz_shade"

    _clip_float_wgs(dem, bb, float_wgs)
    _warp_webmerc_float(float_wgs, float_merc, upsample=_UPSAMPLE)
    elev, weight, water, _transform, profile, keep = _process_mesh(float_merc, bb)
    (elev, weight, water, keep), profile = _crop_to_mask(
        [elev, weight, water, keep], keep, profile
    )
    omit = keep < 0.5
    _write_geotiff(processed, elev, profile=profile, count=1)

    if not shade_only:
        _write_rgb_geotiff(elev, profile, rgb_merc, omit=omit)
        _gdal2tiles(rgb_merc, xyz_rgb, "near")
        _normalize_xyz(xyz_rgb, "dem")
        _publish_xyz(xyz_rgb, OUT_DIR, merge=merge, skip_existing=skip_existing)
    else:
        print("[terrain-rgb] --shade-only: skipping Terrain-RGB tiles", flush=True)

    try:
        _gdaldem_hillshade(processed, hs_tif)
        _write_shade_rgba(hs_tif, weight, profile, shade_rgba, water=water)
        _gdal2tiles(shade_rgba, xyz_shade, "bilinear")
    except subprocess.CalledProcessError:
        print("[terrain-rgb] RGBA shade tiles failed — retrying as grey+alpha", flush=True)
        import rasterio  # noqa: PLC0415
        from rasterio.enums import ColorInterp  # noqa: PLC0415

        with rasterio.open(hs_tif) as src:
            hs = src.read(1)
        ga = np.stack([hs, np.where(weight > 0.05, 255, 0).astype(np.uint8)])
        _write_geotiff(
            shade_rgba,
            ga,
            profile=profile,
            count=2,
            colorinterp=[ColorInterp.grey, ColorInterp.alpha],
        )
        _gdal2tiles(shade_rgba, xyz_shade, "bilinear")
    _normalize_xyz(xyz_shade, "shade")
    _publish_xyz(xyz_shade, SHADE_OUT_DIR, merge=merge, skip_existing=skip_existing)

    bounds = _wgs_bounds_of(processed)
    print(f"[terrain-rgb] processed bounds={bounds}", flush=True)
    if fill_missing:
        _fill_missing_shade_tiles(SHADE_OUT_DIR, bounds)
    # Gap fills must not scrub: a finer coastline can sit inland of the old
    # mask, and scrubbing would delete sea tiles that are already on disk.
    if merge and not skip_existing:
        _scrub_xyz_away_from_coast(OUT_DIR, bb)
        _scrub_xyz_away_from_coast(SHADE_OUT_DIR, bb)
    _scrub_gdal2tiles_junk(OUT_DIR)
    _scrub_gdal2tiles_junk(SHADE_OUT_DIR)
    return bounds


def main() -> None:
    force = "--force" in sys.argv
    shade_only = "--shade-only" in sys.argv
    if "--normalize-only" in sys.argv:
        print("[terrain-rgb] --normalize-only", flush=True)
        _normalize_xyz(OUT_DIR, "dem")
        _normalize_xyz(SHADE_OUT_DIR, "shade")
        bounds = None
        if META_OUT.exists():
            meta = json.loads(META_OUT.read_text(encoding="utf-8"))
            bounds = meta.get("bounds")
            meta["version"] = _TILEJSON_VERSION
            META_OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            print(f"[terrain-rgb] bumped {META_OUT} version={_TILEJSON_VERSION}")
        if SHADE_META_OUT.exists():
            meta = json.loads(SHADE_META_OUT.read_text(encoding="utf-8"))
            bounds = bounds or meta.get("bounds")
            meta["version"] = _TILEJSON_VERSION
            SHADE_META_OUT.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if bounds and _bbox_span_deg2(
            {
                "lon_min": float(bounds[0]),
                "lat_min": float(bounds[1]),
                "lon_max": float(bounds[2]),
                "lat_max": float(bounds[3]),
            }
        ) < 6.0:
            _fill_missing_shade_tiles(SHADE_OUT_DIR, bounds)
        return

    bb = chart_bbox()
    if use_cyclades() and parse_bbox_arg() is None:
        from region import AEGEAN_DEM_BBOX  # noqa: PLC0415

        bb = dict(AEGEAN_DEM_BBOX)

    full_chart = _same_bbox(bb, REGION_BBOX)
    _stamp_prebaked_from_tilejson()

    cogs = _one_degree_cogs(bb)
    if not cogs:
        dem = _find_dem(bb)
        if use_cyclades() and parse_bbox_arg() is None:
            bb = _bbox_of_dem(dem)
        print(
            f"[terrain-rgb] DEM={dem.name} bbox={bb} upsample={_UPSAMPLE}x "
            f"z{_MINZOOM}-{_MAXZOOM}",
            flush=True,
        )
        WORK.mkdir(parents=True, exist_ok=True)
        fill = _bbox_span_deg2(bb) < 6.0
        bounds = _bake_dem_bbox(
            dem,
            bb,
            shade_only=shade_only,
            merge=False,
            fill_missing=fill,
            work=WORK / "single",
        )
        _write_meta(bounds, dem.name)
        sea_mask = OUT_DIR / "sea_mask.geojson"
        if sea_mask.exists():
            sea_mask.unlink()
        print(f"[terrain-rgb] tiles → {OUT_DIR} (z{_MINZOOM}–{_MAXZOOM})")
        print(f"[terrain-rgb] shade → {SHADE_OUT_DIR}")
        return

    if full_chart:
        _write_meta(
            (
                REGION_BBOX["lon_min"],
                REGION_BBOX["lat_min"],
                REGION_BBOX["lon_max"],
                REGION_BBOX["lat_max"],
            ),
            "REGION_BBOX 1° GLO-30",
        )

    print(
        f"[terrain-rgb] {len(cogs)} 1° COG(s) in bbox={bb} "
        f"upsample={_UPSAMPLE}x z{_MINZOOM}-{_MAXZOOM} force={force}",
        flush=True,
    )
    baked = skipped = ocean = inland = 0
    for i, dem in enumerate(cogs, start=1):
        tile_bb = _cog_bbox(dem)
        if tile_bb is None:
            continue
        if not force and _is_done(dem.stem):
            note = _done_path(dem.stem).read_text(encoding="utf-8").strip()
            if note in {"baked", "prebaked"}:
                _scrub_xyz_away_from_coast(OUT_DIR, tile_bb)
                _scrub_xyz_away_from_coast(SHADE_OUT_DIR, tile_bb)
                _mark_done(dem.stem, f"{note}:strip-scrubbed")
            skipped += 1
            print(f"[terrain-rgb] {i}/{len(cogs)} skip done {dem.stem}", flush=True)
            continue
        if not bbox_near_coast(tile_bb):
            try:
                _mark_done(dem.stem, "skip:inland")
            except OSError as exc:
                print(f"[terrain-rgb] stamp failed {dem.stem}: {exc}", flush=True)
                break
            inland += 1
            print(f"[terrain-rgb] {i}/{len(cogs)} skip inland {dem.stem}", flush=True)
            continue
        if not _cog_has_orography(dem):
            try:
                _mark_done(dem.stem, "skip:ocean")
            except OSError as exc:
                print(f"[terrain-rgb] stamp failed {dem.stem}: {exc}", flush=True)
                break
            ocean += 1
            print(f"[terrain-rgb] {i}/{len(cogs)} skip ocean {dem.stem}", flush=True)
            continue
        print(f"[terrain-rgb] {i}/{len(cogs)} bake {dem.stem} {tile_bb}", flush=True)
        free_gb = shutil.disk_usage(OUT_DIR).free / (1024 ** 3)
        if free_gb < 4.0:
            print(
                f"[terrain-rgb] stopping — only {free_gb:.1f} GB free (need ≥ 4 GB)",
                flush=True,
            )
            break
        work = WORK / dem.stem
        try:
            _bake_dem_bbox(
                dem,
                tile_bb,
                shade_only=shade_only,
                merge=True,
                fill_missing=False,
                work=work,
            )
            _mark_done(dem.stem, "baked:strip")
            baked += 1
            if not full_chart:
                _write_meta(
                    (bb["lon_min"], bb["lat_min"], bb["lon_max"], bb["lat_max"]),
                    f"{len(cogs)} 1° GLO-30",
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[terrain-rgb] FAILED {dem.stem}: {exc}", flush=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    sea_mask = OUT_DIR / "sea_mask.geojson"
    if sea_mask.exists():
        sea_mask.unlink()
        print("[terrain-rgb] removed unused sea_mask.geojson")

    pending = [p.stem for p in cogs if not _is_done(p.stem)]
    if full_chart and not pending:
        _REGION_STAMP.write_text(
            f"baked={baked} skipped={skipped} ocean={ocean} inland={inland} cogs={len(cogs)}\n",
            encoding="utf-8",
        )
        print(f"[terrain-rgb] region complete → {_REGION_STAMP}", flush=True)
    print(
        f"[terrain-rgb] done baked={baked} skipped={skipped} ocean={ocean} "
        f"inland={inland} pending={len(pending)} → {OUT_DIR}",
        flush=True,
    )


if __name__ == "__main__":
    main()
