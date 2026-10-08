#!/usr/bin/env python3
"""Offline WindNinja terrain-response library bake.

Builds a per-direction-sector day/night speedup+deflection library on the
~400 m WindNinja mesh and stores it as Zarr under data/cache/wind_terrain/.

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
import fcntl
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

# Allow running as `python scripts/build_wind_terrain.py` from services/api/
_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent  # services/api
_REPO = _SCRIPTS.parents[2]  # monorepo root (MasterWind)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

from region import AEGEAN_DEM_BBOX, chart_bbox  # noqa: E402
from coastal_band import bbox_near_coast, coastal_keep_on_lonlat, crop_keep_slices  # noqa: E402

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
    REGION_LIBRARY,
    REGION_STAMP,
    SECTOR_DEG,
    TERRAIN_DIR,
    TILES_MANIFEST,
    seed_region_manifest,
    upsert_tile_manifest,
    warm_sea_mask,
    write_identity_library,
)
from app.wind.zoom_tiers import WIND_CANON_RES_DEG  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("build_wind_terrain")

TILE_DEG = 2.0  # Cyclades-sized cells for the full chart
HALO_DEG = 0.05  # ~5 km
REF_SPEED_MPS = 10.0
# Concurrent WindNinja CLI processes. Each CLI is num_threads=1 — extra
# in-process threads cost RAM on a 16 GB machine that is already swapping.
_WN_PARALLEL_DEFAULT = 3
_WN_CLI_THREADS = 1
# WindNinja DEM + solver mesh (library lattice matches this).
_WN_MESH_M = 400.0
_WN_RES_DEG = _WN_MESH_M / 111_320.0


def _bbox_from_dict(d: dict[str, float]) -> BBox:
    return BBox(
        lat_min=d["lat_min"],
        lon_min=d["lon_min"],
        lat_max=d["lat_max"],
        lon_max=d["lon_max"],
    )


def _tile_grid(region: BBox) -> list[tuple[BBox, BBox]]:
    """Return (core, halo) 2° tiles covering region."""
    tiles: list[tuple[BBox, BBox]] = []
    lat = region.lat_min
    while lat < region.lat_max - 1e-9:
        lon = region.lon_min
        lat_hi = min(lat + TILE_DEG, region.lat_max)
        while lon < region.lon_max - 1e-9:
            lon_hi = min(lon + TILE_DEG, region.lon_max)
            core = BBox(
                lat_min=round(lat, 4),
                lon_min=round(lon, 4),
                lat_max=round(lat_hi, 4),
                lon_max=round(lon_hi, 4),
            )
            halo = BBox(
                lat_min=round(lat - HALO_DEG, 4),
                lon_min=round(lon - HALO_DEG, 4),
                lat_max=round(lat_hi + HALO_DEG, 4),
                lon_max=round(lon_hi + HALO_DEG, 4),
            )
            tiles.append((core, halo))
            lon = lon_hi
        lat = lat_hi
    return tiles


def _tile_dirname(core: BBox) -> str:
    lat0 = int(math.floor(core.lat_min))
    lon0 = int(math.floor(core.lon_min))
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"tile_{ew}{abs(lon0):03d}_{ns}{abs(lat0):02d}"


def _gdalbuildvrt() -> str | None:
    found = shutil.which("gdalbuildvrt")
    if found:
        return found
    for base in (
        "/Applications/Postgres.app/Contents/Versions/latest/bin",
        "/opt/homebrew/bin",
    ):
        p = Path(base) / "gdalbuildvrt"
        if p.exists():
            return str(p)
    return None


def _cog_sw(path: Path) -> tuple[int, int] | None:
    name = path.stem
    # Copernicus_DSM_COG_10_N37_00_E024_00_DEM
    parts = name.split("_")
    try:
        i = parts.index("10")
        ns, nlat, _z1, ew, nlon = parts[i + 1 : i + 6]
        lat = int(nlat)
        lon = int(nlon)
        if ns.upper() == "S":
            lat = -lat
        if ew.upper() == "W":
            lon = -lon
        return lat, lon
    except (ValueError, IndexError):
        return None


def _covering_dems(bbox: BBox) -> list[Path]:
    dem_dir = DATA_DIR / "dem"
    vrt = dem_dir / "region_dem.vrt"
    if vrt.exists():
        return [vrt]
    out: list[Path] = []
    for path in sorted(dem_dir.glob("Copernicus_DSM_*.tif")):
        sw = _cog_sw(path)
        if sw is None:
            continue
        lat, lon = sw
        if (
            lon + 1 < bbox.lon_min
            or lon > bbox.lon_max
            or lat + 1 < bbox.lat_min
            or lat > bbox.lat_max
        ):
            continue
        if path.stat().st_size > 100_000:
            out.append(path)
    if out:
        return out
    for name in (
        "region_dem.tif",
        "region_dem_custom.tif",
        "region_dem_cyclades.tif",
        "region_dem_sample.tif",
    ):
        p = dem_dir / name
        if p.exists():
            out.append(p)
    return out


def _bbox_has_orography(bbox: BBox) -> bool:
    srcs = _covering_dems(bbox)
    if not srcs:
        return False
    try:
        import rasterio  # noqa: PLC0415
        from rasterio.enums import Resampling  # noqa: PLC0415
        from rasterio.windows import from_bounds  # noqa: PLC0415

        peak = -1e9
        valid_n = 0
        for src_path in srcs:
            with rasterio.open(src_path) as ds:
                win = from_bounds(
                    bbox.lon_min,
                    bbox.lat_min,
                    bbox.lon_max,
                    bbox.lat_max,
                    transform=ds.transform,
                )
                win = win.round_offsets().round_lengths()
                if win.width < 2 or win.height < 2:
                    continue
                h = min(int(win.height), 64)
                w = min(int(win.width), 64)
                data = ds.read(
                    1,
                    window=win,
                    out_shape=(h, w),
                    resampling=Resampling.bilinear,
                )
                arr = np.asarray(data, dtype=np.float32)
                mask = ~np.isfinite(arr) | (arr <= -1000.0)
                if ds.nodata is not None:
                    mask |= arr == ds.nodata
                valid = arr[~mask]
                if valid.size:
                    valid_n += int(valid.size)
                    peak = max(peak, float(np.nanmax(valid)))
        return valid_n >= 20 and peak >= 1.5
    except Exception as exc:  # noqa: BLE001
        logger.debug("orography probe failed: %s", exc)
        return True


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


def _write_dem_geotiff(
    path: Path,
    bbox: BBox,
    lats: np.ndarray,
    lons: np.ndarray,
    keep: np.ndarray | None = None,
) -> str | None:
    """Write a UTM DEM GeoTIFF for WindNinja; return CRS string or None on failure.

    WindNinja rejects geographic (EPSG:4326) DEMs — must be projected.
    Pixels outside ``keep`` are flattened to 0 m so the solver does not model
    inland hills we will not store.
    """
    srcs = _covering_dems(bbox)
    if not srcs:
        logger.warning("no DEM covering %s — skipping WindNinja DEM write", bbox)
        return None
    src_path = srcs[0]
    tmp_vrt: Path | None = None
    if len(srcs) > 1:
        vrt_bin = _gdalbuildvrt()
        if vrt_bin:
            tmp_vrt = path.parent / "cover.vrt"
            try:
                subprocess.run(
                    [vrt_bin, "-overwrite", str(tmp_vrt), *[str(p) for p in srcs]],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                src_path = tmp_vrt
            except subprocess.CalledProcessError as exc:
                logger.warning("gdalbuildvrt failed (%s) — using %s", exc, srcs[0].name)
                src_path = srcs[0]
    try:
        import rasterio  # noqa: PLC0415
        from rasterio.transform import from_bounds  # noqa: PLC0415
        from rasterio.warp import (  # noqa: PLC0415
            Resampling,
            calculate_default_transform,
            reproject,
        )
        from rasterio.windows import Window  # noqa: PLC0415
        from rasterio.windows import transform as window_transform  # noqa: PLC0415

        mid_lon = 0.5 * (bbox.lon_min + bbox.lon_max)
        mid_lat = 0.5 * (bbox.lat_min + bbox.lat_max)
        dst_crs = f"EPSG:{_utm_epsg(mid_lon, mid_lat)}"

        # Target WindNinja mesh in projected metres covering the geographic bbox.
        res_m = _WN_MESH_M
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
        with rasterio.open(src_path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=elev,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
        if keep is not None and keep.shape == (lats.size, lons.size):
            keep_utm = np.zeros((height, width), dtype=np.float32)
            keep_n = np.flipud(np.asarray(keep, dtype=np.float32))
            src_transform = from_bounds(
                float(lons[0]),
                float(lats[0]),
                float(lons[-1]),
                float(lats[-1]),
                int(lons.size),
                int(lats.size),
            )
            reproject(
                source=keep_n,
                destination=keep_utm,
                src_transform=src_transform,
                src_crs="EPSG:4326",
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
            )
            elev[keep_utm < 0.5] = 0.0
            rows = np.any(keep_utm >= 0.5, axis=1)
            cols = np.any(keep_utm >= 0.5, axis=0)
            if np.any(rows) and np.any(cols):
                r_idx = np.flatnonzero(rows)
                c_idx = np.flatnonzero(cols)
                pad = 2
                r0 = max(0, int(r_idx[0]) - pad)
                r1 = min(height, int(r_idx[-1]) + 1 + pad)
                c0 = max(0, int(c_idx[0]) - pad)
                c1 = min(width, int(c_idx[-1]) + 1 + pad)
                if r1 - r0 >= 8 and c1 - c0 >= 8:
                    win = Window(c0, r0, c1 - c0, r1 - r0)
                    transform = window_transform(win, transform)
                    elev = elev[r0:r1, c0:c1]
                    height, width = elev.shape
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
            src_path.name,
        )
        return dst_crs
    except Exception as exc:  # noqa: BLE001
        logger.warning("DEM GeoTIFF write failed: %s", exc)
        return None
    finally:
        if tmp_vrt is not None:
            tmp_vrt.unlink(missing_ok=True)


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
    env["OMP_NUM_THREADS"] = str(_WN_CLI_THREADS)
    env["VECLIB_MAXIMUM_THREADS"] = str(_WN_CLI_THREADS)
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
        f"num_threads = {_WN_CLI_THREADS}",
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
    # Own process group so a timeout can SIGKILL even if the CLI is in
    # uninterruptible disk wait (U) and communicate() cannot reap it.
    try:
        proc = subprocess.Popen(
            [bin_path, str(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=_windninja_env(),
        )
    except FileNotFoundError:
        logger.error("WindNinja binary not found: %s", bin_path)
        return None
    try:
        stdout, stderr = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        logger.error(
            "WindNinja timed out dir=%s — killing process group %s",
            out_dir.name,
            proc.pid,
        )
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None
    if proc.returncode != 0:
        err = (stderr or "")[-2000:]
        out = (stdout or "")[-1000:]
        logger.error(
            "WindNinja failed code=%s\nstderr=%s\nstdout=%s",
            proc.returncode,
            err,
            out,
        )
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
    tile: BBox, bin_path: str, out_zarr: Path, res: float, *, workers: int
) -> BBox | None:
    lats, lons = _axes(tile, res)
    keep = coastal_keep_on_lonlat(lons, lats)
    if keep is None:
        keep = np.ones((lats.size, lons.size), dtype=bool)
    elif not np.any(keep):
        logger.info("no coastal strip in tile — skip WindNinja")
        return None
    from scipy.ndimage import binary_dilation  # noqa: PLC0415

    # ~5 km solver pad so stored 20 km cells are not against the domain wall.
    pad_cells = max(1, int(round(5_000.0 / _WN_MESH_M)))
    keep_solve = binary_dilation(keep, iterations=pad_cells)
    sl_solve = crop_keep_slices(keep_solve, pad=1)
    if sl_solve is None:
        logger.info("coastal strip too small to mesh — skip WindNinja")
        return None
    slat, slon = sl_solve
    lats = lats[slat]
    lons = lons[slon]
    keep = keep[slat, slon]
    keep_solve = keep_solve[slat, slon]
    tile = BBox(
        lat_min=float(lats[0]),
        lon_min=float(lons[0]),
        lat_max=float(lats[-1]),
        lon_max=float(lons[-1]),
    )
    logger.info(
        "coastal strip lattice %dx%d (keep=%d%% solve-pad=%d cells)",
        lats.size,
        lons.size,
        int(100.0 * keep.mean()),
        pad_cells,
    )
    with tempfile.TemporaryDirectory(prefix="wn_tile_") as tmp:
        tmp_path = Path(tmp)
        dem = tmp_path / "dem.tif"
        dem_crs = _write_dem_geotiff(dem, tile, lats, lons, keep=keep_solve)
        if dem_crs is None:
            return None
        nlat, nlon = lats.size, lons.size
        shape = (N_SECTORS, 2, nlat, nlon)
        speedup = np.ones(shape, dtype=np.float32)
        deflection = np.zeros(shape, dtype=np.float32)
        z0 = roughness_grid(tile, lats, lons)
        rough_su = roughness_speedup_factor(z0)
        ok_n = 0
        mesh_m = _WN_MESH_M

        def _wn_job(s: int, day_i: int, diurnal: bool) -> tuple:
            direction = s * SECTOR_DEG
            out_dir = tmp_path / f"s{s}_{day_i}"
            out_dir.mkdir(exist_ok=True)
            vel = _run_windninja(
                bin_path,
                dem,
                out_dir,
                direction,
                diurnal=diurnal,
                mesh_res_m=mesh_m,
            )
            return s, day_i, diurnal, direction, vel, out_dir

        jobs = [
            (s, day_i, diurnal)
            for s in range(N_SECTORS)
            for day_i, diurnal in enumerate((True, False))
        ]
        logger.info(
            "WindNinja %d runs (%d sectors × day/night) workers=%d mesh=%.0fm",
            len(jobs),
            N_SECTORS,
            workers,
            mesh_m,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_wn_job, s, day_i, diurnal) for s, day_i, diurnal in jobs
            ]
            for fut in as_completed(futs):
                s, day_i, diurnal, direction, vel, out_dir = fut.result()
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
                    "sector %d/%d day=%s ok (speedup std=%.3f) %d/%d",
                    s + 1,
                    N_SECTORS,
                    diurnal,
                    float(speedup[s, day_i].std()),
                    ok_n,
                    len(jobs),
                )
        if ok_n == 0:
            logger.error("WindNinja produced no successful sector runs")
            return None
        inland = ~keep
        if np.any(inland):
            speedup[:, :, inland] = 1.0
            deflection[:, :, inland] = 0.0
        sl_store = crop_keep_slices(keep, pad=1)
        if sl_store is not None:
            s_lat, s_lon = sl_store
            lats = lats[s_lat]
            lons = lons[s_lon]
            speedup = speedup[:, :, s_lat, s_lon]
            deflection = deflection[:, :, s_lat, s_lon]
        stored = BBox(
            lat_min=float(lats[0]),
            lon_min=float(lons[0]),
            lat_max=float(lats[-1]),
            lon_max=float(lons[-1]),
        )
        if not _write_zarr(out_zarr, lats, lons, speedup, deflection, source="windninja"):
            return None
        return stored


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
            "res_deg": float(abs(lats[1] - lats[0])) if lats.size > 1 else _WN_RES_DEG,
            "source": source,
            "n_sectors": N_SECTORS,
            "coastal_strip_km": 20.0,
        },
    )
    ds.to_zarr(path, mode="w", zarr_format=2)
    logger.info("wrote %s (%dx%d)", path, lats.size, lons.size)
    try:
        warm_sea_mask(path, lats, lons)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sea-mask sidecar failed: %s", exc)
    return True


def _upsert_tile_locked(rel_path: str, bbox: BBox, source: str) -> None:
    """Process-safe tiles.json update (two tile workers can finish together)."""
    TERRAIN_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TERRAIN_DIR / ".tiles.lock"
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        upsert_tile_manifest(rel_path, bbox, source)


def _bbox_as_dict(b: BBox) -> dict[str, float]:
    return {
        "lat_min": b.lat_min,
        "lon_min": b.lon_min,
        "lat_max": b.lat_max,
        "lon_max": b.lon_max,
    }


def _process_one_tile(payload: tuple) -> str:
    """Module-level so ProcessPoolExecutor can pickle it under spawn."""
    i, n_tiles, core_d, halo_d, bin_path, workers = payload
    core = _bbox_from_dict(core_d)
    halo = _bbox_from_dict(halo_d)
    name = _tile_dirname(core)
    logger.info("tile %d/%d %s core=%s", i + 1, n_tiles, name, core)
    tile_out = TERRAIN_DIR / name / "library.zarr"
    stored = _bake_tile_with_windninja(
        halo, bin_path, tile_out, _WN_RES_DEG, workers=workers
    )
    if stored:
        _upsert_tile_locked(f"{name}/library.zarr", stored, "windninja")
        return "ok"
    return "fail"


def _core_already_baked(core: BBox) -> bool:
    tile_zarr = TERRAIN_DIR / _tile_dirname(core) / "library.zarr"
    if tile_zarr.exists():
        return True
    seed_region_manifest()
    if not TILES_MANIFEST.exists():
        return False
    try:
        entries = json.loads(TILES_MANIFEST.read_text(encoding="utf-8")).get("tiles", [])
    except json.JSONDecodeError:
        return False
    pad = 0.05
    for e in entries:
        if e.get("source") == "identity":
            continue
        try:
            if (
                core.lon_min >= float(e["lon_min"]) - pad
                and core.lat_min >= float(e["lat_min"]) - pad
                and core.lon_max <= float(e["lon_max"]) + pad
                and core.lat_max <= float(e["lat_max"]) + pad
            ):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--identity",
        action="store_true",
        help="Write a unit-speedup library (no WindNinja required; small bbox only)",
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
        help="Bake the four GLO-30 1° tiles (24–26°E, 37–39°N)",
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
        help="Output Zarr path for a single-mosaic bake",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Rebake tiles even when a covering library already exists",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("WN_PARALLEL", str(_WN_PARALLEL_DEFAULT))),
        help="Concurrent WindNinja CLI processes per 2° tile (default 3; 2 tiles → 6 CLIs)",
    )
    ap.add_argument(
        "--tile-workers",
        type=int,
        default=int(os.environ.get("WN_TILE_WORKERS", "2")),
        help="2° tiles to bake at once (each runs --workers WindNinja CLIs)",
    )
    args = ap.parse_args()
    workers = max(1, min(16, args.workers))
    tile_workers = max(1, min(4, args.tile_workers))
    region = _bbox_from_dict(chart_bbox())
    if args.cyclades and args.bbox is None:
        region = _bbox_from_dict(AEGEAN_DEM_BBOX)

    sources = chart_sources_present()
    logger.info("chart sources: %s", sources)
    logger.info("region %s", region)

    # Touch roughness once so missing sources are logged early.
    lats, lons = _axes(region, WIND_CANON_RES_DEG * 8)  # coarse probe
    _ = roughness_grid(region, lats, lons)

    span = (region.lat_max - region.lat_min) * (region.lon_max - region.lon_min)
    seed_region_manifest()

    if args.identity or not args.windninja_bin:
        if span >= 5.0 and not args.identity:
            logger.error(
                "WindNinja binary not found — refusing to write an identity stub "
                "over the full chart. Install WindNinja and re-run."
            )
            return 1
        if span >= 5.0:
            logger.error("refusing --identity for a region ≥ 5 deg²")
            return 1
        if not args.identity and not args.windninja_bin:
            logger.warning(
                "WindNinja binary not found — writing identity library. "
                "Install WindNinja and re-run with --windninja-bin for real terrain response."
            )
        write_identity_library(args.out, region, res_deg=WIND_CANON_RES_DEG)
        logger.info("identity library ready at %s", args.out)
        return 0

    # Small clip: one mosaic (Cyclades 2×2° is 4 deg²).
    if span < 5.0:
        stored = _bake_tile_with_windninja(
            region, args.windninja_bin, args.out, _WN_RES_DEG, workers=workers
        )
        if stored:
            try:
                rel = str(Path(args.out).resolve().relative_to(TERRAIN_DIR.resolve()))
            except ValueError:
                rel = "region/library.zarr"
            upsert_tile_manifest(rel, stored, "windninja")
        return 0 if stored else 1

    tiles = _tile_grid(region)
    logger.info(
        "baking %d 2° tiles with WindNinja (skip ocean / inland / already baked) "
        "tile-workers=%d workers=%d",
        len(tiles),
        tile_workers,
        workers,
    )
    ok_n = skipped = ocean = inland = 0
    jobs: list[tuple] = []
    for i, (core, halo) in enumerate(tiles):
        if not args.force and _core_already_baked(core):
            skipped += 1
            continue
        if not bbox_near_coast(
            {
                "lon_min": core.lon_min,
                "lat_min": core.lat_min,
                "lon_max": core.lon_max,
                "lat_max": core.lat_max,
            }
        ):
            inland += 1
            continue
        if not _bbox_has_orography(core):
            ocean += 1
            continue
        jobs.append(
            (i, len(tiles), _bbox_as_dict(core), _bbox_as_dict(halo), args.windninja_bin, workers)
        )
    logger.info(
        "queued %d tiles (skipped=%d inland=%d ocean=%d)",
        len(jobs),
        skipped,
        inland,
        ocean,
    )
    if jobs:
        pool_n = min(tile_workers, len(jobs))
        with ProcessPoolExecutor(max_workers=pool_n) as pool:
            futs = [pool.submit(_process_one_tile, p) for p in jobs]
            for fut in as_completed(futs):
                try:
                    status = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.error("tile bake crashed: %s", exc)
                    continue
                if status == "ok":
                    ok_n += 1
                else:
                    logger.error("tile bake failed")

    leftover = len(tiles) - ok_n - skipped - ocean - inland
    logger.info(
        "baked %d skipped=%d ocean=%d inland=%d pending=%d / %d",
        ok_n,
        skipped,
        ocean,
        inland,
        leftover,
        len(tiles),
    )
    if leftover == 0:
        REGION_STAMP.write_text(
            json.dumps(
                {
                    "baked": ok_n,
                    "skipped": skipped,
                    "ocean": ocean,
                    "inland": inland,
                    "tiles": len(tiles),
                    "tile_deg": TILE_DEG,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info("region complete → %s", REGION_STAMP)
    if ok_n == 0 and skipped == 0:
        logger.error("no tiles baked successfully")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
