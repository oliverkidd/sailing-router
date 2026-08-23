#!/usr/bin/env python3
"""Offline WindNinja terrain-response library bake.

Builds a per-direction-sector day/night speedup+deflection library on the
200 m lattice and stores it as Zarr under data/cache/wind_terrain/.

Usage:
  # Identity library (no WindNinja install) — for wiring / tests:
  python -m scripts.build_wind_terrain --identity --sample

  # Full bake (requires WindNinja CLI on PATH or --windninja-bin):
  python -m scripts.build_wind_terrain --windninja-bin /path/to/WindNinja

WindNinja is GPL and is NOT linked into the API — this script shells out
offline only. Consent required before installing WindNinja/Windmapper.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

# Allow running as `python scripts/build_wind_terrain.py` from services/api/
_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent  # services/api
_REPO = _SCRIPTS.parents[3]  # monorepo root (MasterWind)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

from region import chart_bbox  # noqa: E402

from app.config import DATA_DIR  # noqa: E402
from app.schemas import BBox  # noqa: E402
from app.wind.roughness import (  # noqa: E402
    chart_sources_present,
    roughness_grid,
    roughness_speedup_factor,
)
from app.wind.terrain_library import (  # noqa: E402
    LIBRARY_VERSION,
    N_SECTORS,
    SECTOR_DEG,
    TERRAIN_DIR,
    write_identity_library,
)
from app.wind.zoom_tiers import WIND_CANON_RES_DEG  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("build_wind_terrain")

TILE_DEG = 0.4  # ~40 km at mid-lat
HALO_DEG = 0.05  # ~5 km
REF_SPEED_MPS = 10.0


def _bbox_from_dict(d: dict[str, float]) -> BBox:
    return BBox(
        lat_min=d["lat_min"],
        lon_min=d["lon_min"],
        lat_max=d["lat_max"],
        lon_max=d["lon_max"],
    )


def _tile_grid(region: BBox) -> list[BBox]:
    tiles: list[BBox] = []
    lat = region.lat_min
    while lat < region.lat_max - 1e-9:
        lon = region.lon_min
        lat_hi = min(lat + TILE_DEG, region.lat_max)
        while lon < region.lon_max - 1e-9:
            lon_hi = min(lon + TILE_DEG, region.lon_max)
            tiles.append(
                BBox(
                    lat_min=round(lat - HALO_DEG, 4),
                    lon_min=round(lon - HALO_DEG, 4),
                    lat_max=round(lat_hi + HALO_DEG, 4),
                    lon_max=round(lon_hi + HALO_DEG, 4),
                )
            )
            lon = lon_hi
        lat = lat_hi
    return tiles


def _axes(bbox: BBox, res: float) -> tuple[np.ndarray, np.ndarray]:
    lat0 = math.floor(bbox.lat_min / res) * res
    lon0 = math.floor(bbox.lon_min / res) * res
    nlat = max(2, int(round((bbox.lat_max - lat0) / res)) + 1)
    nlon = max(2, int(round((bbox.lon_max - lon0) / res)) + 1)
    return lat0 + np.arange(nlat) * res, lon0 + np.arange(nlon) * res


def _utm_epsg(lon: float, lat: float) -> int:
    """UTM EPSG code for a lon/lat (northern or southern hemisphere)."""
    zone = int((lon + 180) // 6) + 1
    return (32700 if lat < 0 else 32600) + zone


def _write_dem_geotiff(path: Path, bbox: BBox, lats: np.ndarray, lons: np.ndarray) -> str | None:
    """Write a UTM DEM GeoTIFF for WindNinja; return CRS string or None on failure.

    WindNinja rejects geographic (EPSG:4326) DEMs — must be projected.
    """
    dem_dir = DATA_DIR / "dem"
    preferred = [
        dem_dir / "region_dem_cyclades.tif",
        dem_dir / "region_dem_custom.tif",
        dem_dir / "region_dem_sample.tif",
        dem_dir / "region_dem.tif",
    ]
    tifs = [p for p in preferred if p.exists()]
    tifs += [
        p
        for p in sorted(dem_dir.glob("*.tif")) + sorted(dem_dir.glob("*.tiff"))
        if p not in tifs
    ]
    if not tifs:
        logger.warning("no DEM GeoTIFF in %s — skipping WindNinja DEM write", dem_dir)
        return None
    try:
        import rasterio  # noqa: PLC0415
        from rasterio.warp import (  # noqa: PLC0415
            Resampling,
            calculate_default_transform,
            reproject,
        )

        mid_lon = 0.5 * (bbox.lon_min + bbox.lon_max)
        mid_lat = 0.5 * (bbox.lat_min + bbox.lat_max)
        dst_crs = f"EPSG:{_utm_epsg(mid_lon, mid_lat)}"

        # Target ~200 m cells in projected metres covering the geographic bbox.
        res_m = max(50.0, float(WIND_CANON_RES_DEG) * 111_320.0)
        transform, width, height = calculate_default_transform(
            "EPSG:4326",
            dst_crs,
            100,
            100,
            left=bbox.lon_min,
            bottom=bbox.lat_min,
            right=bbox.lon_max,
            top=bbox.lat_max,
            resolution=res_m,
        )
        elev = np.zeros((height, width), dtype=np.float32)
        with rasterio.open(tifs[0]) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=elev,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs=dst_crs,
            transform=transform,
        ) as dst:
            dst.write(elev, 1)
        logger.info(
            "DEM for WindNinja %s %dx%d (%.0fm) from %s",
            dst_crs,
            width,
            height,
            res_m,
            tifs[0].name,
        )
        return dst_crs
    except Exception as exc:  # noqa: BLE001
        logger.warning("DEM GeoTIFF write failed: %s", exc)
        return None


def _windninja_env() -> dict[str, str]:
    """Env for WindNinja CLI (needs WINDNINJA_DATA + GDAL_DATA; no PROJ network)."""
    env = os.environ.copy()
    data = _REPO / "tools" / "windninja" / "data"
    if not data.is_dir():
        data = _REPO / "tools" / "windninja" / "src" / "windninja" / "data"
    if data.is_dir():
        env.setdefault("WINDNINJA_DATA", str(data))
    for candidate in (
        Path("/opt/homebrew/share/gdal"),
        Path("/usr/local/share/gdal"),
    ):
        if candidate.is_dir():
            env.setdefault("GDAL_DATA", str(candidate))
            break
    for candidate in (
        Path("/opt/homebrew/share/proj"),
        Path("/usr/local/share/proj"),
    ):
        if candidate.is_dir():
            env.setdefault("PROJ_DATA", str(candidate))
            env.setdefault("PROJ_LIB", str(candidate))
            break
    # Avoid GDAL/PROJ phone-home (ninjastorm / cdn) during offline bake.
    env["PROJ_NETWORK"] = "OFF"
    env["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
    return env


def _run_windninja(
    bin_path: str,
    dem_tif: Path,
    out_dir: Path,
    direction_deg: float,
    *,
    diurnal: bool,
    mesh_res_m: float = 200.0,
) -> Path | None:
    """Invoke WindNinja CLI; return path to speed ASCII grid if produced."""
    cfg = out_dir / f"wn_{int(direction_deg)}_{'day' if diurnal else 'night'}.cfg"
    # domainAverageInitialization — projected DEM required; match example diurnal fields.
    lines = [
        "num_threads = 2",
        f"elevation_file = {dem_tif}",
        "initialization_method = domainAverageInitialization",
        f"input_speed = {REF_SPEED_MPS}",
        "input_speed_units = mps",
        f"input_direction = {direction_deg}",
        "input_wind_height = 10",
        "units_input_wind_height = m",
        "output_wind_height = 10",
        "units_output_wind_height = m",
        "output_speed_units = mps",
        "vegetation = grass",
        f"diurnal_winds = {'true' if diurnal else 'false'}",
        "time_zone = Europe/Athens",
        "year = 2026",
        "month = 7",
        "day = 18",
        f"hour = {12 if diurnal else 2}",
        "minute = 0",
        "uni_air_temp = 28.0",
        "air_temp_units = C",
        "uni_cloud_cover = 20.0",
        "cloud_cover_units = percent",
        f"mesh_resolution = {mesh_res_m}",
        "units_mesh_resolution = m",
        "write_goog_output = false",
        "write_shapefile_output = false",
        "write_ascii_output = true",
        f"ascii_out_resolution = {mesh_res_m}",
        "units_ascii_out_resolution = m",
        "write_farsite_atm = false",
        f"output_path = {out_dir}/",
    ]
    cfg.write_text("\n".join(lines) + "\n")
    try:
        subprocess.run(
            [bin_path, str(cfg)],
            check=True,
            capture_output=True,
            text=True,
            timeout=900,
            env=_windninja_env(),
        )
    except FileNotFoundError:
        logger.error("WindNinja binary not found: %s", bin_path)
        return None
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "")[-2000:]
        out = (exc.stdout or "")[-1000:]
        logger.error("WindNinja failed: %s\nstderr=%s\nstdout=%s", exc, err, out)
        return None
    except subprocess.TimeoutExpired:
        logger.error("WindNinja timed out")
        return None
    asciis = sorted(out_dir.glob("*vel*.asc"), key=lambda p: p.stat().st_mtime)
    return asciis[-1] if asciis else None


def _read_ascii_grid(path: Path) -> tuple[np.ndarray, dict]:
    """Read an Arc/Info ASCII grid → (2d array, header dict)."""
    header: dict[str, float] = {}
    with path.open() as f:
        for _ in range(6):
            key, val = f.readline().split()
            header[key.lower()] = float(val)
        data = np.loadtxt(f, dtype=np.float32)
    return data, header


def _resample_ascii_to_latlon(
    data: np.ndarray,
    header: dict,
    src_crs: str,
    lats: np.ndarray,
    lons: np.ndarray,
    *,
    fill_value: float = REF_SPEED_MPS,
    src_nodata: float | None = None,
) -> np.ndarray:
    """Warp a projected ASCII grid onto the ascending lat/lon lattice."""
    from rasterio.transform import Affine, from_bounds  # noqa: PLC0415
    from rasterio.warp import Resampling, reproject  # noqa: PLC0415

    nrows = int(header.get("nrows", data.shape[0]))
    cell = float(header["cellsize"])
    if "xllcorner" in header:
        x0 = float(header["xllcorner"])
        y0 = float(header["yllcorner"])
        transform = Affine(cell, 0.0, x0, 0.0, -cell, y0 + nrows * cell)
    else:
        x0 = float(header["xllcenter"]) - cell / 2.0
        y0 = float(header["yllcenter"]) - cell / 2.0
        transform = Affine(cell, 0.0, x0, 0.0, -cell, y0 + nrows * cell)

    nlat, nlon = lats.size, lons.size
    dst = np.full((nlat, nlon), np.nan, dtype=np.float32)
    # north-up destination for rasterio, then flip to ascending south→north lats
    dst_transform = from_bounds(
        float(lons[0]),
        float(lats[0]),
        float(lons[-1]),
        float(lats[-1]),
        nlon,
        nlat,
    )
    nodata = (
        float(header.get("nodata_value", -9999))
        if src_nodata is None
        else float(src_nodata)
    )
    reproject(
        source=data.astype(np.float32),
        destination=dst,
        src_transform=transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
        src_nodata=nodata,
        dst_nodata=np.nan,
    )
    if lats[0] < lats[-1]:
        dst = dst[::-1, :]
    out = np.where(np.isfinite(dst), dst, fill_value).astype(np.float32)
    return out


def _resample_angle_ascii_to_latlon(
    data: np.ndarray,
    header: dict,
    src_crs: str,
    lats: np.ndarray,
    lons: np.ndarray,
    *,
    direction_deg: float,
) -> np.ndarray:
    """Warp an angular ASCII grid using wrap-safe sin/cos bilinear resampling.

    WindNinja ``ang`` fields are degrees in [0, 360). Bilinearly resampling raw
    degrees across the 0/360 wrap (e.g. north sector) interpolates 359° with 1°
    toward ~180° — garbage deflection. Resample unit-vector components instead.
    """
    nodata = float(header.get("nodata_value", -9999))
    ang = np.asarray(data, dtype=np.float64)
    valid = np.isfinite(ang) & (ang != nodata)
    fill_rad = math.radians(float(direction_deg))
    rad = np.where(valid, np.radians(ang), fill_rad)
    sin_src = np.sin(rad).astype(np.float32)
    cos_src = np.cos(rad).astype(np.float32)
    # Components are in [-1, 1]; disable nodata masking during warp.
    sin_dst = _resample_ascii_to_latlon(
        sin_src,
        header,
        src_crs,
        lats,
        lons,
        fill_value=math.sin(fill_rad),
        src_nodata=-1.0e30,
    )
    cos_dst = _resample_ascii_to_latlon(
        cos_src,
        header,
        src_crs,
        lats,
        lons,
        fill_value=math.cos(fill_rad),
        src_nodata=-1.0e30,
    )
    return np.degrees(np.arctan2(sin_dst, cos_dst)).astype(np.float32)


def _bake_tile_with_windninja(
    tile: BBox, bin_path: str, out_zarr: Path, res: float
) -> bool:
    lats, lons = _axes(tile, res)
    with tempfile.TemporaryDirectory(prefix="wn_tile_") as tmp:
        tmp_path = Path(tmp)
        dem = tmp_path / "dem.tif"
        dem_crs = _write_dem_geotiff(dem, tile, lats, lons)
        if dem_crs is None:
            return False
        nlat, nlon = lats.size, lons.size
        shape = (N_SECTORS, 2, nlat, nlon)
        speedup = np.ones(shape, dtype=np.float32)
        deflection = np.zeros(shape, dtype=np.float32)
        z0 = roughness_grid(tile, lats, lons)
        rough_su = roughness_speedup_factor(z0)
        ok_n = 0
        mesh_m = max(50.0, float(res) * 111_320.0)
        for s in range(N_SECTORS):
            direction = s * SECTOR_DEG
            for day_i, diurnal in enumerate((True, False)):
                out_dir = tmp_path / f"s{s}_{day_i}"
                out_dir.mkdir()
                vel = _run_windninja(
                    bin_path,
                    dem,
                    out_dir,
                    direction,
                    diurnal=diurnal,
                    mesh_res_m=mesh_m,
                )
                if vel is None:
                    continue
                data, hdr = _read_ascii_grid(vel)
                data = _resample_ascii_to_latlon(data, hdr, dem_crs, lats, lons)
                oro = np.clip(data / REF_SPEED_MPS, 0.05, 5.0)
                speedup[s, day_i] = np.clip(oro * rough_su, 0.05, 5.0)
                ang = list(out_dir.glob("*ang*.asc"))
                if ang:
                    adata, ahdr = _read_ascii_grid(ang[0])
                    adata = _resample_angle_ascii_to_latlon(
                        adata,
                        ahdr,
                        dem_crs,
                        lats,
                        lons,
                        direction_deg=direction,
                    )
                    deflection[s, day_i] = ((adata - direction + 180) % 360) - 180
                ok_n += 1
                logger.info(
                    "sector %d/%d day=%s ok (speedup std=%.3f)",
                    s + 1,
                    N_SECTORS,
                    diurnal,
                    float(speedup[s, day_i].std()),
                )
        if ok_n == 0:
            logger.error("WindNinja produced no successful sector runs")
            return False
        return _write_zarr(out_zarr, lats, lons, speedup, deflection, source="windninja")


def _write_zarr(
    path: Path,
    lats: np.ndarray,
    lons: np.ndarray,
    speedup: np.ndarray,
    deflection: np.ndarray,
    *,
    source: str,
) -> bool:
    import xarray as xr  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.rmtree(path)
    ds = xr.Dataset(
        data_vars={
            "speedup": (("sector", "daynight", "lat", "lon"), speedup),
            "deflection_deg": (("sector", "daynight", "lat", "lon"), deflection),
        },
        coords={
            "sector": np.arange(N_SECTORS),
            "daynight": np.array([0, 1]),
            "lat": lats.astype(float),
            "lon": lons.astype(float),
        },
        attrs={
            "version": LIBRARY_VERSION,
            "res_deg": WIND_CANON_RES_DEG,
            "source": source,
            "n_sectors": N_SECTORS,
        },
    )
    ds.to_zarr(path, mode="w", zarr_format=2)
    logger.info("wrote %s (%dx%d)", path, lats.size, lons.size)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--identity",
        action="store_true",
        help="Write a unit-speedup library (no WindNinja required)",
    )
    ap.add_argument(
        "--windninja-bin",
        default=(
            shutil.which("WindNinja")
            or shutil.which("WindNinja_cli")
            or shutil.which("windninja")
            or (
                str(_REPO / "tools" / "windninja" / "bin" / "WindNinja")
                if (_REPO / "tools" / "windninja" / "bin" / "WindNinja").exists()
                else None
            )
            or (
                str(_REPO / "tools" / "windninja" / "bin" / "WindNinja_cli")
                if (_REPO / "tools" / "windninja" / "bin" / "WindNinja_cli").exists()
                else None
            )
        ),
        help="Path to WindNinja CLI binary",
    )
    ap.add_argument("--sample", action="store_true", help="Bake Solent SAMPLE_BBOX only")
    ap.add_argument(
        "--cyclades",
        action="store_true",
        help="Bake Kea/Kythnos/Syros CYCLADES_BBOX only",
    )
    ap.add_argument(
        "--bbox",
        default=None,
        help="Custom lon_min,lat_min,lon_max,lat_max (overrides --sample/--cyclades)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=TERRAIN_DIR / "region" / "library.zarr",
        help="Output Zarr path",
    )
    args = ap.parse_args()
    region = _bbox_from_dict(chart_bbox())

    sources = chart_sources_present()
    logger.info("chart sources: %s", sources)
    logger.info("region %s → %s", region, args.out)

    # Touch roughness once so missing sources are logged early.
    lats, lons = _axes(region, WIND_CANON_RES_DEG * 8)  # coarse probe
    _ = roughness_grid(region, lats, lons)

    if args.identity or not args.windninja_bin:
        if not args.identity and not args.windninja_bin:
            logger.warning(
                "WindNinja binary not found — writing identity library. "
                "Install WindNinja and re-run with --windninja-bin for real terrain response."
            )
        write_identity_library(args.out, region, res_deg=WIND_CANON_RES_DEG)
        logger.info("identity library ready at %s", args.out)
        return 0

    # Full region: bake as one mosaic for sample/cyclades/small; tile for full region.
    if (region.lat_max - region.lat_min) * (region.lon_max - region.lon_min) < 2.0:
        ok = _bake_tile_with_windninja(
            region, args.windninja_bin, args.out, WIND_CANON_RES_DEG
        )
        return 0 if ok else 1

    tiles = _tile_grid(region)
    logger.info("baking %d tiles with WindNinja", len(tiles))
    ok_n = 0
    for i, tile in enumerate(tiles):
        tile_out = TERRAIN_DIR / f"tile_{i:04d}" / "library.zarr"
        logger.info("tile %d/%d %s", i + 1, len(tiles), tile)
        if _bake_tile_with_windninja(
            tile, args.windninja_bin, tile_out, WIND_CANON_RES_DEG
        ):
            ok_n += 1
    # Also write a region-level identity stub pointing consumers at tiles.
    if ok_n == 0:
        logger.error("no tiles baked successfully")
        return 1
    write_identity_library(args.out, region, res_deg=WIND_CANON_RES_DEG * 8)
    logger.info("baked %d/%d tiles; coarse region stub at %s", ok_n, len(tiles), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
