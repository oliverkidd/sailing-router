#!/usr/bin/env python3
"""Build depth.pmtiles (labelled isobath lines) for the UK+Europe chart region.

Sole bathymetry source: EMODnet Bathymetry DTM via WCS (ows.emodnet-bathymetry.eu).
No ETOPO / GEBCO fallback — missing EMODnet exits non-zero.

Requires: tippecanoe, gdal_contour, ogr2ogr, gdalbuildvrt, gdal_translate;
          rasterio + numpy in the API venv.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import DEPTH_LEVELS_M, EMODNET_RES_DEG, REGION_BBOX  # noqa: E402

BATHY_DIR = ROOT / "data" / "geo" / "bathymetry"
REGION_TIF = BATHY_DIR / "region_depth.tif"
WORK = ROOT / "data" / "cache" / "tiles_build"
CHUNKS_DIR = WORK / "emodnet_chunks"
OUT = ROOT / "apps" / "web" / "public" / "geo" / "depth.pmtiles"

_WCS_BASE = "https://ows.emodnet-bathymetry.eu/wcs"
# Chunk size in degrees — EMODnet WCS rejects reads above ~98 MB;
# native res over ~3°×3° exceeds that, so stay at 2.5°.
_CHUNK_DEG = 2.5
# Reject stale ETOPO / low-res rasters (EMODnet native ≈ 0.00104°).
_MAX_PIXEL_DEG = 0.003


def _wcs_url(west: float, south: float, east: float, north: float) -> str:
    res = EMODNET_RES_DEG
    return (
        f"{_WCS_BASE}?service=wcs&version=1.0.0&request=getcoverage"
        f"&coverage=emodnet:mean&crs=EPSG:4326"
        f"&BBOX={west},{south},{east},{north}"
        f"&format=image/tiff&interpolation=nearest"
        f"&resx={res}&resy={res}"
    )


def _elev_to_depth_tif(src: Path, dst: Path) -> None:
    """WCS emodnet:mean is elevation (positive up). Depth = -elev over water."""
    import rasterio  # noqa: PLC0415

    with rasterio.open(src) as ds:
        elev = ds.read(1).astype(np.float32)
        profile = ds.profile.copy()

    # Nodata / land → nan so contours stop at the wet edge.
    depth = np.where(elev < 0, -elev, np.nan).astype(np.float32)
    profile.update(dtype="float32", nodata=np.nan, compress="deflate")
    with rasterio.open(dst, "w", **profile) as out:
        out.write(depth, 1)


def _download_emodnet_region() -> Path:
    """Fetch EMODnet via tiled WCS, mosaic to region_depth.tif (positive-down)."""
    import rasterio  # noqa: PLC0415

    BATHY_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

    lon0 = REGION_BBOX["lon_min"]
    lat0 = REGION_BBOX["lat_min"]
    lon1 = REGION_BBOX["lon_max"]
    lat1 = REGION_BBOX["lat_max"]

    n_lon = max(1, math.ceil((lon1 - lon0) / _CHUNK_DEG))
    n_lat = max(1, math.ceil((lat1 - lat0) / _CHUNK_DEG))
    depth_chunks: list[Path] = []

    print(
        f"[depth] fetching EMODnet WCS ({n_lon}×{n_lat} chunks @ "
        f"{EMODNET_RES_DEG:.6f}°) for REGION_BBOX …"
    )

    for iy in range(n_lat):
        south = lat0 + iy * _CHUNK_DEG
        north = min(lat1, south + _CHUNK_DEG)
        for ix in range(n_lon):
            west = lon0 + ix * _CHUNK_DEG
            east = min(lon1, west + _CHUNK_DEG)
            if east <= west or north <= south:
                continue

            raw = CHUNKS_DIR / f"elev_{iy:02d}_{ix:02d}.tif"
            depth = CHUNKS_DIR / f"depth_{iy:02d}_{ix:02d}.tif"
            if depth.exists() and depth.stat().st_size > 1000:
                depth_chunks.append(depth)
                continue

            url = _wcs_url(west, south, east, north)
            print(f"[depth]   chunk [{iy},{ix}] {west:.2f},{south:.2f}→{east:.2f},{north:.2f}")
            try:
                urllib.request.urlretrieve(url, raw)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(
                    f"EMODnet WCS download failed for chunk ({west},{south},{east},{north}): {exc}"
                ) from exc

            if not raw.exists() or raw.stat().st_size < 500:
                body = raw.read_text(errors="replace") if raw.exists() else ""
                raise RuntimeError(
                    f"EMODnet WCS returned empty/error for {raw.name}: {body[:300]}"
                )

            # Reject HTML/XML error bodies (e.g. size-limit ServiceException).
            head = raw.read_bytes()[:400]
            if head.lstrip().startswith(b"<?xml") or b"ServiceException" in head:
                raise RuntimeError(
                    f"EMODnet WCS error response for {raw.name}: "
                    f"{head.decode('utf-8', errors='replace')[:400]}"
                )

            try:
                with rasterio.open(raw) as ds:
                    _ = ds.width
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"EMODnet WCS did not return a readable GeoTIFF ({raw}): {exc}"
                ) from exc

            _elev_to_depth_tif(raw, depth)
            try:
                raw.unlink()
            except OSError:
                pass
            depth_chunks.append(depth)

    if not depth_chunks:
        raise RuntimeError("No EMODnet depth chunks produced")

    vrt = CHUNKS_DIR / "region_depth.vrt"
    if vrt.exists():
        vrt.unlink()
    subprocess.run(
        ["gdalbuildvrt", "-overwrite", str(vrt), *[str(p) for p in depth_chunks]],
        check=True,
    )

    if REGION_TIF.exists():
        REGION_TIF.unlink()
    print(f"[depth] mosaicking → {REGION_TIF} …")
    subprocess.run(
        [
            "gdal_translate",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            "-co",
            "BIGTIFF=IF_SAFER",
            str(vrt),
            str(REGION_TIF),
        ],
        check=True,
    )
    print(f"[depth] wrote {REGION_TIF} ({REGION_TIF.stat().st_size / 1e6:.1f} MB)")
    return REGION_TIF


def _tif_looks_like_emodnet(path: Path) -> bool:
    import rasterio  # noqa: PLC0415

    with rasterio.open(path) as ds:
        px = abs(ds.transform.a)
        py = abs(ds.transform.e)
    return px <= _MAX_PIXEL_DEG and py <= _MAX_PIXEL_DEG


def _ensure_region_tif() -> Path:
    if REGION_TIF.exists() and REGION_TIF.stat().st_size > 10_000:
        if _tif_looks_like_emodnet(REGION_TIF):
            print(f"[depth] using existing EMODnet-resolution {REGION_TIF}")
            return REGION_TIF
        print(
            f"[depth] rejecting low-res/stale {REGION_TIF} "
            f"(need EMODnet ≤ {_MAX_PIXEL_DEG}°); refetching …"
        )
        REGION_TIF.unlink()
    return _download_emodnet_region()


def _contour_to_geojsonl(tif: Path, geojsonl: Path) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    gpkg = WORK / "depth_contours.gpkg"
    if gpkg.exists():
        gpkg.unlink()

    cmd = [
        "gdal_contour",
        "-a",
        "depth",
        "-fl",
        *[str(v) for v in DEPTH_LEVELS_M],
        str(tif),
        str(gpkg),
    ]
    print("[depth] gdal_contour:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    raw = WORK / "depth_contours.geojsonl"
    if raw.exists():
        raw.unlink()
    subprocess.run(
        ["ogr2ogr", "-f", "GeoJSONSeq", str(raw), str(gpkg)],
        check=True,
    )

    n = 0
    with raw.open() as inp, geojsonl.open("w") as out:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            props = feat.get("properties") or {}
            depth = props.get("depth", props.get("ELEV", props.get("elev")))
            try:
                depth_f = float(depth)
            except (TypeError, ValueError):
                continue
            if depth_f <= 0:
                continue
            feat["properties"] = {"depth": int(round(depth_f))}
            out.write(json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
    return n


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    try:
        tif = _ensure_region_tif()
    except Exception as exc:  # noqa: BLE001
        print(
            f"[depth] FAILED to obtain EMODnet bathymetry: {exc}\n"
            "       No ETOPO / GEBCO fallback.",
            file=sys.stderr,
        )
        return 1

    geojsonl = WORK / "depth.geojsonl"
    print("[depth] contouring isobaths (raw gdal_contour, no simplify) …")
    try:
        n = _contour_to_geojsonl(tif, geojsonl)
    except Exception as exc:  # noqa: BLE001
        print(f"[depth] contour failed: {exc}", file=sys.stderr)
        return 1
    print(f"[depth] wrote {n} contour features → {geojsonl}")
    if n == 0:
        print("[depth] no contours — abort", file=sys.stderr)
        return 1

    if OUT.exists():
        OUT.unlink()

    cmd = [
        "tippecanoe",
        "-o",
        str(OUT),
        "-Z4",
        "-z14",
        "--force",
        "--no-tile-size-limit",
        "--no-feature-limit",
        "-l",
        "depth",
        str(geojsonl),
    ]
    print("[depth] tippecanoe:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"[depth] wrote {OUT} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
