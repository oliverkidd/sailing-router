#!/usr/bin/env python3
"""Build depth.pmtiles (isobath lines + shallow fill bands) for the UK+Europe chart.

Recipe soft_noclip_05_1m: EMODnet soft-land contours (incl. 0.5 / 1 m), no OSM land
clip, DP + 2× Chaikin; vector fill bands between 2/4/5/10/50 m isobaths.

Sole bathymetry source: EMODnet Bathymetry DTM via WCS (ows.emodnet-bathymetry.eu).
No ETOPO / GEBCO fallback — missing EMODnet exits non-zero.

Requires: tippecanoe, gdal_contour, ogr2ogr, gdalbuildvrt, gdal_translate;
          rasterio + numpy + shapely in the API venv.
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

from region import (  # noqa: E402
    DEPTH_LEVELS_M,
    EMODNET_RES_DEG,
    REGION_BBOX,
    archive_output,
)

BATHY_DIR = ROOT / "data" / "geo" / "bathymetry"
REGION_TIF = BATHY_DIR / "region_depth.tif"
MOSAIC_STAMP = BATHY_DIR / "region_depth.stamp"
WORK = ROOT / "data" / "cache" / "tiles_build"
CHUNKS_DIR = WORK / "emodnet_chunks"
OUT = ROOT / "apps" / "web" / "public" / "geo" / "depth.pmtiles"

_WCS_BASE = "https://ows.emodnet-bathymetry.eu/wcs"
# Chunk size in degrees — EMODnet WCS rejects reads above ~98 MB;
# native res over ~3°×3° exceeds that, so stay at 2.5°.
_CHUNK_DEG = 2.5
# Overlap so isobaths do not break on 2.5° mosaic seams (~20 native pixels).
_CHUNK_OVERLAP_DEG = 0.02
# Bump when fetch/mosaic params change so stale nearest/no-overlap caches are ignored.
_MOSAIC_TAG = "bilinear_ol02"
# Reject stale ETOPO / low-res rasters (EMODnet native ≈ 0.00104°).
_MAX_PIXEL_DEG = 0.003
_WCS_TIMEOUT_S = 120
_WCS_RETRIES = 4

# Post-contour cleanup — soft_noclip_05_1m (lab winner): soft land, no OSM clip,
# DP + 2× Chaikin, keep short harbour fragments; vector fill bands between isobaths.
_SIMPLIFY_TOL_DEG = 0.0003
_CHAIKIN_ITERS = 2
_MIN_LINE_POINTS = 4
_MIN_LINE_LENGTH_DEG = 0.0002
_RECIPE_TAG = "soft_noclip_05_1m"

# Fill bands from these isobath edges (+ region/chunk frame).
_BAND_EDGE_DEPTHS_M = (2.0, 4.0, 5.0, 10.0, 50.0)
_SHALLOW_BANDS = (
    (0.0, 2.0, "0-2"),
    (2.0, 4.0, "2-4"),
    (4.0, 5.0, "4-5"),
    (5.0, 10.0, "5-10"),
    (10.0, 50.0, "10-50"),
    (50.0, 1.0e6, "50+"),
)
_BAND_CHUNK_DEG = 2.5

# Per-isobath minzoom: deep contours appear when zoomed out (coarse offshore),
# shallow/harbour contours only when zoomed in — keeps low-zoom tiles light
# without changing the zoomed-in look.
_MAXZOOM = 14


def _isobath_minzoom(depth_m: float) -> int:
    d = abs(float(depth_m))
    if d <= 1.0:
        return 12
    if d <= 2.0:
        return 11
    if d <= 5.0:
        return 9
    if d <= 10.0:
        return 7
    if d <= 20.0:
        return 6
    return 4


# Per-band minzoom for the fill wash: keep coarse deep bands at low zoom so the
# sea still reads blue when zoomed out; gate the fine shallow bands to high zoom.
_FILL_BAND_MINZOOM = {
    "0-2": 9,
    "2-4": 9,
    "4-5": 9,
    "5-10": 7,
    "10-50": 5,
    "50+": 4,
}

OSM_SHP_CANDIDATES = [
    ROOT / "data" / "geo" / "osm" / "land-polygons-split-4326" / "land_polygons.shp",
    ROOT / "data" / "geo" / "osm" / "land_polygons.shp",
]


def _wcs_url(west: float, south: float, east: float, north: float) -> str:
    res = EMODNET_RES_DEG
    return (
        f"{_WCS_BASE}?service=wcs&version=1.0.0&request=getcoverage"
        f"&coverage=emodnet:mean&crs=EPSG:4326"
        f"&BBOX={west},{south},{east},{north}"
        f"&format=image/tiff&interpolation=bilinear"
        f"&resx={res}&resy={res}"
    )


def _find_osm_shp() -> Path | None:
    for p in OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = sorted(
        (ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"),
        key=lambda p: (0 if "split" in str(p) else 1, str(p)),
    )
    return matches[0] if matches else None


def _elev_to_depth_tif(src: Path, dst: Path) -> None:
    """WCS emodnet:mean is elevation (positive up). Depth = -elev over water.

    Block-windowed so a chunk (or mosaic) is never fully materialised in RAM.
    """
    import rasterio  # noqa: PLC0415

    with rasterio.open(src) as ds:
        profile = ds.profile.copy()
        profile.update(dtype="float32", nodata=np.nan, compress="deflate")
        with rasterio.open(dst, "w", **profile) as out:
            for _, window in ds.block_windows(1):
                elev = ds.read(1, window=window).astype(np.float32)
                depth = np.where(elev < 0, -elev, np.nan).astype(np.float32)
                out.write(depth, 1, window=window)


def _write_soft_land_tif(src: Path, dst: Path) -> Path:
    """Land/NaN → depth 0 so shallow isobaths stay continuous at the wet edge."""
    import rasterio  # noqa: PLC0415

    if dst.exists():
        dst.unlink()
    print(f"[depth] soft_land NaN→0 → {dst} …", flush=True)
    with rasterio.open(src) as ds:
        profile = ds.profile.copy()
        profile.update(dtype="float32", nodata=None, compress="deflate")
        with rasterio.open(dst, "w", **profile) as out:
            for _, window in ds.block_windows(1):
                data = ds.read(1, window=window).astype(np.float32)
                soft = np.where(np.isfinite(data), data, 0.0).astype(np.float32)
                out.write(soft, 1, window=window)
    return dst


def _chaikin(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    pts = list(coords)
    for _ in range(iterations):
        if len(pts) < 3:
            break
        nxt: list[tuple[float, float]] = [pts[0]]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            nxt.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            nxt.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        nxt.append(pts[-1])
        pts = nxt
    return pts


def _chunk_bbox(ix: int, iy: int, lon0: float, lat0: float, lon1: float, lat1: float) -> tuple[float, float, float, float]:
    """Core 2.5° cell expanded by overlap, clipped to region."""
    south = max(lat0, lat0 + iy * _CHUNK_DEG - _CHUNK_OVERLAP_DEG)
    north = min(lat1, lat0 + (iy + 1) * _CHUNK_DEG + _CHUNK_OVERLAP_DEG)
    west = max(lon0, lon0 + ix * _CHUNK_DEG - _CHUNK_OVERLAP_DEG)
    east = min(lon1, lon0 + (ix + 1) * _CHUNK_DEG + _CHUNK_OVERLAP_DEG)
    return west, south, east, north


def _download_emodnet_region() -> Path:
    """Fetch EMODnet via overlapped WCS chunks, mosaic to region_depth.tif."""
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
        f"{EMODNET_RES_DEG:.6f}°, bilinear, overlap={_CHUNK_OVERLAP_DEG}°) …"
    )

    for iy in range(n_lat):
        for ix in range(n_lon):
            west, south, east, north = _chunk_bbox(ix, iy, lon0, lat0, lon1, lat1)
            if east <= west or north <= south:
                continue

            # Tag filenames so nearest / no-overlap caches are never reused.
            raw = CHUNKS_DIR / f"elev_{_MOSAIC_TAG}_{iy:02d}_{ix:02d}.tif"
            depth = CHUNKS_DIR / f"depth_{_MOSAIC_TAG}_{iy:02d}_{ix:02d}.tif"
            if depth.exists() and depth.stat().st_size > 1000:
                depth_chunks.append(depth)
                continue

            url = _wcs_url(west, south, east, north)
            print(
                f"[depth]   chunk [{iy},{ix}] "
                f"{west:.2f},{south:.2f}→{east:.2f},{north:.2f}",
                flush=True,
            )
            last_exc: Exception | None = None
            for attempt in range(1, _WCS_RETRIES + 1):
                try:
                    if raw.exists():
                        raw.unlink()
                    with urllib.request.urlopen(url, timeout=_WCS_TIMEOUT_S) as resp, raw.open(
                        "wb"
                    ) as out:
                        while True:
                            block = resp.read(1 << 20)
                            if not block:
                                break
                            out.write(block)
                    last_exc = None
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_exc = exc
                    print(
                        f"[depth]   chunk [{iy},{ix}] attempt {attempt}/"
                        f"{_WCS_RETRIES} failed: {exc}",
                        flush=True,
                    )
            if last_exc is not None:
                raise RuntimeError(
                    f"EMODnet WCS download failed for chunk "
                    f"({west},{south},{east},{north}): {last_exc}"
                ) from last_exc

            if not raw.exists() or raw.stat().st_size < 500:
                body = raw.read_text(errors="replace") if raw.exists() else ""
                raise RuntimeError(
                    f"EMODnet WCS returned empty/error for {raw.name}: {body[:300]}"
                )

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

    vrt = CHUNKS_DIR / f"region_depth_{_MOSAIC_TAG}.vrt"
    if vrt.exists():
        vrt.unlink()
    # Later chunks win in overlap strips (stable isobaths across seams).
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
    MOSAIC_STAMP.write_text(_MOSAIC_TAG + "\n", encoding="utf-8")
    print(f"[depth] wrote {REGION_TIF} ({REGION_TIF.stat().st_size / 1e6:.1f} MB)")
    return REGION_TIF


def _tif_looks_like_emodnet(path: Path) -> bool:
    import rasterio  # noqa: PLC0415

    with rasterio.open(path) as ds:
        px = abs(ds.transform.a)
        py = abs(ds.transform.e)
    return px <= _MAX_PIXEL_DEG and py <= _MAX_PIXEL_DEG


def _mosaic_stamp_ok() -> bool:
    if not MOSAIC_STAMP.exists():
        return False
    return MOSAIC_STAMP.read_text(encoding="utf-8").strip() == _MOSAIC_TAG


def _ensure_region_tif() -> Path:
    if (
        REGION_TIF.exists()
        and REGION_TIF.stat().st_size > 10_000
        and _tif_looks_like_emodnet(REGION_TIF)
        and _mosaic_stamp_ok()
    ):
        print(f"[depth] using existing EMODnet-resolution {REGION_TIF} ({_MOSAIC_TAG})")
        return REGION_TIF
    if REGION_TIF.exists():
        print(
            f"[depth] rejecting stale mosaic (need tag {_MOSAIC_TAG}); refetching …"
        )
        REGION_TIF.unlink()
    return _download_emodnet_region()

def _load_land_index():
    """Simplified OSM land as STRtree (no continent-wide unary_union — avoids OOM)."""
    from shapely import STRtree  # noqa: PLC0415
    from shapely.geometry import shape  # noqa: PLC0415

    shp = _find_osm_shp()
    if shp is None:
        print("[depth] OSM land missing — skipping land clip", file=sys.stderr)
        return None, None

    land_path = WORK / "depth_land_simple.geojsonl"
    need = not land_path.exists() or land_path.stat().st_size < 1000
    if need:
        if land_path.exists():
            land_path.unlink()
        bbox = (
            f"{REGION_BBOX['lon_min']} {REGION_BBOX['lat_min']} "
            f"{REGION_BBOX['lon_max']} {REGION_BBOX['lat_max']}"
        )
        cmd = [
            "ogr2ogr",
            "-f",
            "GeoJSONSeq",
            "-clipsrc",
            *bbox.split(),
            "-simplify",
            "0.02",
            "-nlt",
            "PROMOTE_TO_MULTI",
            str(land_path),
            str(shp),
        ]
        print("[depth] building simplified OSM land index …", flush=True)
        subprocess.run(cmd, check=True)
    else:
        print(f"[depth] reusing land geojsonl {land_path}", flush=True)

    geoms = []
    with land_path.open() as inp:
        for line in inp:
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
            if not g.is_empty:
                geoms.append(g)
    print(f"[depth] land index {len(geoms)} polygons", flush=True)
    if not geoms:
        return None, None
    return STRtree(geoms), geoms


def _iter_line_geoms(geom):
    """Yield LineString pieces from a shapely geometry."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type == "MultiLineString":
        for g in geom.geoms:
            if not g.is_empty:
                yield g
    elif geom.geom_type == "GeometryCollection":
        for g in geom.geoms:
            yield from _iter_line_geoms(g)


def _depth_prop(depth_f: float) -> float | int:
    if abs(depth_f - round(depth_f)) < 1e-6:
        return int(round(depth_f))
    return round(depth_f, 2)


def _clean_contour_feature(feat: dict) -> list[dict]:
    """Simplify + Chaikin; no land clip (lines continue under land / into harbours)."""
    from shapely.geometry import LineString, mapping, shape  # noqa: PLC0415

    props = feat.get("properties") or {}
    depth = props.get("depth", props.get("ELEV", props.get("elev")))
    try:
        depth_f = float(depth)
    except (TypeError, ValueError):
        return []
    if depth_f <= 0:
        return []
    depth_v = _depth_prop(depth_f)

    try:
        line = shape(feat["geometry"])
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for g in _iter_line_geoms(line):
        if len(g.coords) < _MIN_LINE_POINTS:
            continue
        try:
            simplified = g.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=False)
        except Exception:  # noqa: BLE001
            simplified = g
        for piece in _iter_line_geoms(simplified):
            if (
                piece.is_empty
                or piece.geom_type != "LineString"
                or len(piece.coords) < _MIN_LINE_POINTS
                or piece.length < _MIN_LINE_LENGTH_DEG
            ):
                continue
            coords = list(piece.coords)
            if _CHAIKIN_ITERS > 0:
                coords = _chaikin(coords, _CHAIKIN_ITERS)
                if len(coords) < _MIN_LINE_POINTS:
                    continue
                piece = LineString(coords)
            out.append(
                {
                    "type": "Feature",
                    "tippecanoe": {
                        "minzoom": _isobath_minzoom(depth_f),
                        "maxzoom": _MAXZOOM,
                    },
                    "properties": {"depth": depth_v},
                    "geometry": mapping(piece),
                }
            )
    return out


def _classify_band(depth_m: float) -> tuple[float, float, str] | None:
    if not np.isfinite(depth_m) or depth_m < 0:
        return None
    for lo, hi, name in _SHALLOW_BANDS:
        if lo <= depth_m < hi:
            return lo, hi, name
    return None


def _sample_soft_depth(ds, lon: float, lat: float) -> float:
    import rasterio.windows  # noqa: PLC0415

    row, col = ds.index(lon, lat)
    if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
        return float("nan")
    return float(ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0])


def _write_fill_geojsonl(lines_geojsonl: Path, soft: Path, out_geojsonl: Path) -> int:
    """Polygonize fill bands from smoothed 2/4/5/10/50 m isobaths (chunked)."""
    import rasterio  # noqa: PLC0415
    from shapely import STRtree  # noqa: PLC0415
    from shapely.geometry import LineString, box, mapping, shape  # noqa: PLC0415
    from shapely.ops import polygonize, unary_union  # noqa: PLC0415

    edge_depths = {float(d) for d in _BAND_EDGE_DEPTHS_M}
    print("[depth] loading band-edge isobaths for fill polygonize …", flush=True)
    edge_geoms: list = []
    with lines_geojsonl.open() as inp:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            try:
                d = float((feat.get("properties") or {}).get("depth"))
            except (TypeError, ValueError):
                continue
            if d not in edge_depths:
                continue
            try:
                geom = shape(feat["geometry"])
            except Exception:  # noqa: BLE001
                continue
            for g in _iter_line_geoms(geom):
                if not g.is_empty and len(g.coords) >= 2:
                    edge_geoms.append(g)

    print(f"[depth]   {len(edge_geoms)} edge pieces", flush=True)
    if not edge_geoms:
        out_geojsonl.write_text("")
        return 0

    tree = STRtree(edge_geoms)
    lon0 = REGION_BBOX["lon_min"]
    lat0 = REGION_BBOX["lat_min"]
    lon1 = REGION_BBOX["lon_max"]
    lat1 = REGION_BBOX["lat_max"]
    n_lon = max(1, math.ceil((lon1 - lon0) / _BAND_CHUNK_DEG))
    n_lat = max(1, math.ceil((lat1 - lat0) / _BAND_CHUNK_DEG))

    n = 0
    if out_geojsonl.exists():
        out_geojsonl.unlink()
    with rasterio.open(soft) as ds, out_geojsonl.open("w") as out:
        for iy in range(n_lat):
            for ix in range(n_lon):
                west = lon0 + ix * _BAND_CHUNK_DEG
                east = min(lon1, west + _BAND_CHUNK_DEG)
                south = lat0 + iy * _BAND_CHUNK_DEG
                north = min(lat1, south + _BAND_CHUNK_DEG)
                if east <= west or north <= south:
                    continue
                chunk = box(west, south, east, north)
                try:
                    idxs = tree.query(chunk)
                except Exception:  # noqa: BLE001
                    continue
                parts = [edge_geoms[i] for i in idxs]
                if not parts:
                    continue
                parts.append(LineString(chunk.exterior.coords))
                try:
                    noded = unary_union(parts)
                    polys = list(polygonize(noded))
                except Exception:  # noqa: BLE001
                    continue
                for poly in polys:
                    if poly.is_empty or poly.area < 1e-12:
                        continue
                    try:
                        inter = poly.intersection(chunk)
                    except Exception:  # noqa: BLE001
                        continue
                    pieces = []
                    if inter.geom_type == "Polygon":
                        pieces = [inter]
                    elif inter.geom_type == "MultiPolygon":
                        pieces = [g for g in inter.geoms if not g.is_empty]
                    for g in pieces:
                        if g.area < 1e-12:
                            continue
                        pt = g.representative_point()
                        classified = _classify_band(_sample_soft_depth(ds, pt.x, pt.y))
                        if classified is None:
                            continue
                        lo, hi, name = classified
                        feat = {
                            "type": "Feature",
                            "tippecanoe": {
                                "minzoom": _FILL_BAND_MINZOOM.get(name, 4),
                                "maxzoom": _MAXZOOM,
                            },
                            "properties": {
                                "band": name,
                                "depth_lo": lo,
                                "depth_hi": hi,
                            },
                            "geometry": mapping(g),
                        }
                        out.write(json.dumps(feat, separators=(",", ":")) + "\n")
                        n += 1
            print(
                f"[depth]   fill rows {iy + 1}/{n_lat} — {n} band polys so far",
                flush=True,
            )
    return n


def _contour_to_geojsonl(tif: Path, lines_geojsonl: Path) -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    soft = WORK / "region_depth_soft.tif"
    if not soft.exists() or soft.stat().st_size < 1000:
        _write_soft_land_tif(tif, soft)
    else:
        print(f"[depth] reusing soft_land {soft}", flush=True)

    gpkg = WORK / "depth_contours.gpkg"
    raw = WORK / "depth_contours.geojsonl"
    recipe_stamp = WORK / "depth_contours.recipe"
    levels_tag = f"{_RECIPE_TAG}:" + ",".join(str(v) for v in DEPTH_LEVELS_M)
    reuse = (
        gpkg.exists()
        and gpkg.stat().st_size > 1000
        and recipe_stamp.exists()
        and recipe_stamp.read_text(encoding="utf-8").strip() == levels_tag
    )
    if not reuse:
        if gpkg.exists():
            gpkg.unlink()
        cmd = [
            "gdal_contour",
            "-a",
            "depth",
            "-fl",
            *[str(v) for v in DEPTH_LEVELS_M],
            str(soft),
            str(gpkg),
        ]
        print("[depth] gdal_contour (soft_land):", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)
        recipe_stamp.write_text(levels_tag + "\n", encoding="utf-8")
        if raw.exists():
            raw.unlink()
    else:
        print(f"[depth] reusing existing contours gpkg ({levels_tag})", flush=True)

    if not raw.exists() or raw.stat().st_size < 1000:
        if raw.exists():
            raw.unlink()
        subprocess.run(
            ["ogr2ogr", "-f", "GeoJSONSeq", str(raw), str(gpkg)],
            check=True,
        )

    n = 0
    seen = 0
    if lines_geojsonl.exists():
        lines_geojsonl.unlink()
    with raw.open() as inp, lines_geojsonl.open("w") as out:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            seen += 1
            if seen % 50_000 == 0:
                print(
                    f"[depth]   cleaned {seen} raw contours → {n} features …",
                    flush=True,
                )
            feat = json.loads(line)
            for cleaned in _clean_contour_feature(feat):
                out.write(json.dumps(cleaned, separators=(",", ":")) + "\n")
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

    soft = WORK / "region_depth_soft.tif"
    lines_geojsonl = WORK / "depth.geojsonl"
    fill_geojsonl = WORK / "depth_fill.geojsonl"

    print(
        "[depth] contouring isobaths "
        f"({_RECIPE_TAG}: soft_land, no clip, 0.5/1 m, 2× Chaikin) …"
    )
    try:
        n = _contour_to_geojsonl(tif, lines_geojsonl)
    except Exception as exc:  # noqa: BLE001
        print(f"[depth] contour failed: {exc}", file=sys.stderr)
        return 1
    print(f"[depth] wrote {n} contour features → {lines_geojsonl}")
    if n == 0:
        print("[depth] no contours — abort", file=sys.stderr)
        return 1

    if not soft.exists():
        print("[depth] soft mosaic missing — abort", file=sys.stderr)
        return 1

    print("[depth] building vector fill bands (Navionics-style) …", flush=True)
    try:
        n_fill = _write_fill_geojsonl(lines_geojsonl, soft, fill_geojsonl)
    except Exception as exc:  # noqa: BLE001
        print(f"[depth] fill polygonize failed: {exc}", file=sys.stderr)
        return 1
    print(f"[depth] wrote {n_fill} fill features → {fill_geojsonl}")

    archived = archive_output(OUT)
    if archived:
        print(f"[depth] archived previous tiles → {archived}")

    cmd = [
        "tippecanoe",
        "-o",
        str(OUT),
        "-Z4",
        "-z14",
        "--force",
        # Respect the ~500 KB tile limit: simplify low-zoom geometry and drop
        # the densest contours so zoomed-out tiles stay light. Per-feature
        # minzoom (set above) already thins deep vs shallow isobaths by zoom.
        "--simplification=10",
        "--drop-densest-as-needed",
        "-L",
        f"depth:{lines_geojsonl}",
        "-L",
        f"depth_fill:{fill_geojsonl}",
    ]
    print("[depth] tippecanoe:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"[depth] wrote {OUT} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
