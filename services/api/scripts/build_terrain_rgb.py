#!/usr/bin/env python3
"""Build MapLibre Terrain-RGB XYZ tiles from Copernicus DEM for Cyclades (or active bbox).

Encodes GLO-30 elevation as Mapbox Terrain-RGB PNGs under
apps/web/public/geo/terrain/{z}/{x}/{y}.png and writes dem_coverage.geojson
for the map debug outline.

Requires: rasterio, numpy, gdalwarp, gdal2tiles.py on PATH.

Usage:
  python scripts/build_terrain_rgb.py --cyclades
  python scripts/build_terrain_rgb.py --sample
  python scripts/build_terrain_rgb.py --bbox lon_min,lat_min,lon_max,lat_max
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import chart_bbox, use_cyclades, use_sample  # noqa: E402

DEM_DIR = ROOT / "data" / "dem"
WORK = ROOT / "data" / "cache" / "tiles_build" / "terrain_rgb"
OUT_DIR = ROOT / "apps" / "web" / "public" / "geo" / "terrain"
COVERAGE_OUT = ROOT / "apps" / "web" / "public" / "geo" / "dem_coverage.geojson"
META_OUT = OUT_DIR / "tilejson.json"

# MapLibre setTerrain is gated to fine zoom; hillshade useful from mid zoom.
_MINZOOM = 8
_MAXZOOM = 14


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


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = Path(base) / name
        if p.exists():
            return str(p)
    return None


def _encode_terrain_rgb(elev: np.ndarray, nodata_mask: np.ndarray) -> np.ndarray:
    """Mapbox Terrain-RGB: height = -10000 + ((R*256*256 + G*256 + B) * 0.1)."""
    h = elev.astype(np.float64)
    h = np.where(nodata_mask, 0.0, h)  # sea / nodata → 0 m for continuous mesh
    v = np.clip((h + 10000.0) * 10.0, 0.0, 256.0**3 - 1.0)
    r = np.floor(v / 65536.0).astype(np.uint8)
    g = np.floor(np.mod(v, 65536.0) / 256.0).astype(np.uint8)
    b = np.floor(np.mod(v, 256.0)).astype(np.uint8)
    return np.stack([r, g, b], axis=0)


def _write_rgb_geotiff(src_path: Path, bb: dict[str, float], dest: Path) -> tuple[float, float, float, float]:
    """Clip DEM to bbox, encode Terrain-RGB, write WGS84 GeoTIFF. Returns bounds."""
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
        data = src.read(1, window=win)
        transform = window_transform(win, src.transform)
        nodata = src.nodata
        mask = np.isnan(data)
        if nodata is not None:
            mask |= data == nodata
        mask |= data <= -1000.0
        rgb = _encode_terrain_rgb(data, mask)
        profile = src.profile.copy()
        profile.update(
            {
                "driver": "GTiff",
                "count": 3,
                "dtype": "uint8",
                "height": rgb.shape[1],
                "width": rgb.shape[2],
                "transform": transform,
                "nodata": None,
                "compress": "deflate",
                "photometric": "RGB",
            }
        )
        with rasterio.open(dest, "w", **profile) as dst:
            dst.write(rgb)
            for i in range(1, 4):
                dst.set_band_description(i, "terrain-rgb")

        # Exact geographic bounds of written window
        left = transform.c
        top = transform.f
        right = left + transform.a * rgb.shape[2]
        bottom = top + transform.e * rgb.shape[1]
        return (
            min(left, right),
            min(bottom, top),
            max(left, right),
            max(bottom, top),
        )


def _warp_webmerc(src: Path, dest: Path) -> None:
    gdalwarp = _which("gdalwarp")
    if not gdalwarp:
        raise SystemExit("[terrain-rgb] gdalwarp not found on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        gdalwarp,
        "-t_srs",
        "EPSG:3857",
        "-r",
        "bilinear",
        "-of",
        "GTiff",
        "-co",
        "COMPRESS=DEFLATE",
        "-overwrite",
        str(src),
        str(dest),
    ]
    print("[terrain-rgb]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _gdal2tiles(src: Path, out_dir: Path) -> None:
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
        "near",  # preserve Terrain-RGB integer encoding
        "--tilesize=256",
        "--processes=4",
        "--xyz",  # MapLibre / OSM slippy-map numbering (not TMS)
        "-w",
        "none",
        str(src),
        str(out_dir),
    ]
    print("[terrain-rgb]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def _write_meta(
    bounds: tuple[float, float, float, float],
    dem_name: str,
    tile_origin: str = "",
) -> None:
    lon_min, lat_min, lon_max, lat_max = bounds
    feature = {
        "type": "Feature",
        "properties": {"name": dem_name, "kind": "dem"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon_min, lat_min],
                    [lon_max, lat_min],
                    [lon_max, lat_max],
                    [lon_min, lat_max],
                    [lon_min, lat_min],
                ]
            ],
        },
    }
    COVERAGE_OUT.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_OUT.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature]}, indent=2),
        encoding="utf-8",
    )
    META_OUT.parent.mkdir(parents=True, exist_ok=True)
    META_OUT.write_text(
        json.dumps(
            {
                "tilejson": "3.0.0",
                "name": "MasterWind Terrain-RGB",
                "description": f"Mapbox Terrain-RGB from {dem_name}",
                "version": "1.0.0",
                "scheme": "xyz",
                "tiles": [f"{tile_origin}/geo/terrain/{{z}}/{{x}}/{{y}}.png"],
                "minzoom": _MINZOOM,
                "maxzoom": _MAXZOOM,
                "bounds": [lon_min, lat_min, lon_max, lat_max],
                "encoding": "mapbox",
                "tileSize": 256,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[terrain-rgb] wrote {COVERAGE_OUT}")
    print(f"[terrain-rgb] wrote {META_OUT}")


def main() -> None:
    bb = chart_bbox()
    dem = _find_dem(bb)
    print(f"[terrain-rgb] DEM={dem.name} bbox={bb}")

    WORK.mkdir(parents=True, exist_ok=True)
    rgb_wgs = WORK / "terrain_rgb_wgs84.tif"
    rgb_merc = WORK / "terrain_rgb_3857.tif"

    bounds = _write_rgb_geotiff(dem, bb, rgb_wgs)
    print(f"[terrain-rgb] encoded RGB bounds={bounds}")
    _warp_webmerc(rgb_wgs, rgb_merc)
    _gdal2tiles(rgb_merc, OUT_DIR)
    _write_meta(bounds, dem.name)
    # gdal2tiles writes leaflet.html etc. — drop junk, keep z/x/y.png + tilejson
    for junk in OUT_DIR.glob("*.html"):
        junk.unlink(missing_ok=True)
    for junk in ("tilemapresource.xml", "openlayers.html", "googlemaps.html"):
        p = OUT_DIR / junk
        if p.exists():
            p.unlink()
    print(f"[terrain-rgb] tiles → {OUT_DIR} (z{_MINZOOM}–{_MAXZOOM})")


if __name__ == "__main__":
    main()
