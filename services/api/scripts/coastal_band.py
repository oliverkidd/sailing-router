#!/usr/bin/env python3
"""Build a ~10 km sea-coast band from OSM land polygons.

Streaming / disk-backed — never holds Europe-scale land or a continent buffer
in process RAM (that previously peaked ~70 GB in GEOS).

Pipeline:
  1. Ensure land.geojsonl on disk (ogr2ogr clip from OSM shapefile).
  2. One pass: shard land features into per-tile geojsonl files under
     data/cache/tiles_build/coastal_band_tiles/.
  3. Per tile: load only that shard, compute inland strip + seaward buffer,
     append one Feature line to coastal_band_parts.geojsonl, free RAM.
  4. Stream-write FeatureCollection coastal_band.geojson (no giant json.dumps).

Writes:
  data/cache/tiles_build/coastal_band.geojson
"""

from __future__ import annotations

import gc
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import MultiPolygon, box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import REGION_BBOX, chart_bbox, use_sample  # noqa: E402

WORK = ROOT / "data" / "cache" / "tiles_build"
LAND_PATH = WORK / "land.geojsonl"
OUT_PATH = WORK / "coastal_band.geojson"
PARTS_PATH = WORK / "coastal_band_parts.geojsonl"
TILE_DIR = WORK / "coastal_band_tiles"

OSM_SHP_CANDIDATES = [
    ROOT / "data" / "geo" / "osm" / "land-polygons-split-4326" / "land_polygons.shp",
    ROOT / "data" / "geo" / "osm" / "land_polygons.shp",
]

BAND_M = 10_000.0
TILE_DEG = 5.0
# Pad so coastline near tile edges still erodes correctly.
PAD_DEG = 0.2
# ~300 m — keeps Solent inlets, shrinks vertex count a lot.
SIMPLIFY_DEG = 0.003


def _active_bbox() -> dict[str, float]:
    return chart_bbox()


def _find_osm_shp() -> Path | None:
    for p in OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = sorted(
        (ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"),
        key=lambda p: (0 if "split" in str(p) else 1, str(p)),
    )
    return matches[0] if matches else None


def ensure_land_geojsonl() -> Path:
    """Clip OSM land → land.geojsonl via ogr2ogr (disk only; no Python load)."""
    bbox = _active_bbox()
    # Sample clips are tiny; always refresh so we do not reuse a full-Europe file.
    min_bytes = 10_000 if use_sample() else 1_000_000
    if (
        LAND_PATH.exists()
        and LAND_PATH.stat().st_size > min_bytes
        and not use_sample()
    ):
        return LAND_PATH

    shp = _find_osm_shp()
    if shp is None:
        raise FileNotFoundError(
            "OSM land shapefile missing. Run: python scripts/fetch_osm_land.py"
        )

    WORK.mkdir(parents=True, exist_ok=True)
    if LAND_PATH.exists():
        LAND_PATH.unlink()

    clip = (
        f"{bbox['lon_min']} {bbox['lat_min']} "
        f"{bbox['lon_max']} {bbox['lat_max']}"
    )
    cmd = [
        "ogr2ogr",
        "-f",
        "GeoJSONSeq",
        "-clipsrc",
        *clip.split(),
        "-nlt",
        "PROMOTE_TO_MULTI",
        str(LAND_PATH),
        str(shp),
    ]
    label = "SAMPLE Solent" if use_sample() else "REGION"
    print(f"[band] ogr2ogr clip land ({label}) →", LAND_PATH, flush=True)
    subprocess.run(cmd, check=True)
    if not LAND_PATH.exists() or LAND_PATH.stat().st_size < 1000:
        raise RuntimeError(f"ogr2ogr produced empty land clip: {LAND_PATH}")
    return LAND_PATH


def _tile_keys(lon: float, lat: float) -> tuple[int, int]:
    bb = _active_bbox()
    lon0 = bb["lon_min"]
    lat0 = bb["lat_min"]
    return (
        int((lon - lon0) // TILE_DEG),
        int((lat - lat0) // TILE_DEG),
    )


def _iter_tiles() -> list[tuple[int, int, float, float, float, float]]:
    bb = _active_bbox()
    lon0, lat0 = bb["lon_min"], bb["lat_min"]
    lon1, lat1 = bb["lon_max"], bb["lat_max"]
    out: list[tuple[int, int, float, float, float, float]] = []
    lat = lat0
    while lat < lat1 - 1e-9:
        lon = lon0
        north = min(lat + TILE_DEG, lat1)
        while lon < lon1 - 1e-9:
            east = min(lon + TILE_DEG, lon1)
            tx = int((lon - lon0 + 1e-9) // TILE_DEG)
            ty = int((lat - lat0 + 1e-9) // TILE_DEG)
            out.append((tx, ty, lon, lat, east, north))
            lon = east
        lat = north
    return out


def _shard_path(tx: int, ty: int) -> Path:
    return TILE_DIR / f"land_{tx:02d}_{ty:02d}.geojsonl"


def _shard_land_to_disk() -> int:
    """Stream land.geojsonl once; append each feature to intersecting tile shards."""
    if TILE_DIR.exists():
        shutil.rmtree(TILE_DIR)
    TILE_DIR.mkdir(parents=True, exist_ok=True)

    handles: dict[tuple[int, int], object] = {}
    n_in = n_ok = 0
    bb = _active_bbox()
    lon0 = bb["lon_min"]
    lat0 = bb["lat_min"]

    def _fh(key: tuple[int, int]):
        fp = handles.get(key)
        if fp is None:
            fp = _shard_path(*key).open("a")
            handles[key] = fp
        return fp

    try:
        with LAND_PATH.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n_in += 1
                try:
                    feat = json.loads(line)
                    geom = make_valid(shape(feat["geometry"]))
                except Exception:  # noqa: BLE001
                    continue
                if geom.is_empty:
                    continue
                if SIMPLIFY_DEG > 0:
                    geom = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
                    if geom.is_empty:
                        continue
                minx, miny, maxx, maxy = geom.bounds
                tx0 = int((minx - lon0) // TILE_DEG)
                tx1 = int((maxx - lon0) // TILE_DEG)
                ty0 = int((miny - lat0) // TILE_DEG)
                ty1 = int((maxy - lat0) // TILE_DEG)
                keys = {
                    (tx, ty)
                    for tx in range(tx0, tx1 + 1)
                    for ty in range(ty0, ty1 + 1)
                }
                if not keys:
                    keys = {_tile_keys((minx + maxx) / 2, (miny + maxy) / 2)}
                out_line = json.dumps(
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": mapping(geom),
                    },
                    separators=(",", ":"),
                )
                for key in keys:
                    _fh(key).write(out_line + "\n")
                n_ok += 1
                if n_in % 50_000 == 0:
                    print(f"[band] shard pass {n_in} features …", flush=True)
                    gc.collect()
    finally:
        for fp in handles.values():
            fp.close()

    print(
        f"[band] sharded {n_ok}/{n_in} land features → {TILE_DIR} "
        f"({len(handles)} tile files)",
        flush=True,
    )
    return n_ok


def _load_shard_geoms(path: Path) -> list:
    geoms = []
    if not path.exists():
        return geoms
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                g = make_valid(shape(json.loads(line)["geometry"]))
            except Exception:  # noqa: BLE001
                continue
            if not g.is_empty:
                geoms.append(g)
    return geoms


def _chunked_union(geoms: list, chunk: int = 250):
    """Union in batches so fjord/archipelago tiles do not explode GEOS RAM."""
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    parts = []
    for i in range(0, len(geoms), chunk):
        try:
            parts.append(unary_union(geoms[i : i + chunk]))
        except Exception:  # noqa: BLE001
            for g in geoms[i : i + chunk]:
                parts.append(g)
    while len(parts) > 1:
        nxt = []
        for i in range(0, len(parts), chunk):
            try:
                nxt.append(unary_union(parts[i : i + chunk]))
            except Exception:  # noqa: BLE001
                nxt.extend(parts[i : i + chunk])
        parts = nxt
    return parts[0] if parts else None


def _band_from_land(land_u, west, south, east, north, to_metric, to_wgs):
    """Metric inland-strip + seaward expand, clipped back to the tile."""
    if land_u is None or land_u.is_empty:
        return None
    # Extra simplify in WGS before metric buffer — Scandinavian coasts are dense.
    try:
        land_u = land_u.simplify(max(SIMPLIFY_DEG, 0.005), preserve_topology=True)
    except Exception:  # noqa: BLE001
        pass
    land_m = transform(to_metric, land_u)
    try:
        eroded = land_m.buffer(-BAND_M)
        if eroded.is_empty:
            band_m = land_m
        else:
            band_m = land_m.difference(eroded)
        if not band_m.is_empty:
            band_m = make_valid(band_m.buffer(BAND_M))
    except Exception:  # noqa: BLE001
        return None
    if band_m.is_empty:
        return None
    band = transform(to_wgs, band_m).intersection(box(west, south, east, north))
    if band.is_empty:
        return None
    band = make_valid(band)
    return None if band.is_empty else band


def _tile_band(
    geoms: list,
    west: float,
    south: float,
    east: float,
    north: float,
    to_metric,
    to_wgs,
):
    """Inland strip + seaward expand for ONE tile only (bounded GEOS work).

    Dense coast tiles (Norway / Baltic / Greek islands) use a coarse degree
    buffer — the band only gates Overpass, so fjord-perfect geometry is wasted.
    """
    pad = box(west - PAD_DEG, south - PAD_DEG, east + PAD_DEG, north + PAD_DEG)
    clipped = []
    for g in geoms:
        try:
            inter = g.intersection(pad)
        except Exception:  # noqa: BLE001
            continue
        if not inter.is_empty:
            clipped.append(inter)
    if not clipped:
        return None

    n = len(clipped)
    tile_box = box(west, south, east, north)

    # Heavy archipelago tiles: coarse WGS buffer (~10–15 km), no metric CRS.
    if n > 5000:
        simp = []
        for g in clipped:
            try:
                simp.append(g.simplify(0.02, preserve_topology=False))
            except Exception:  # noqa: BLE001
                simp.append(g)
        land_u = _chunked_union(simp, chunk=400)
        if land_u is None or land_u.is_empty:
            return None
        try:
            # ~0.12° ≈ 10–13 km mid-latitudes; generous so Overpass does not miss coast.
            outer = land_u.buffer(0.12)
            inner = land_u.buffer(-0.04)
            band = outer if inner.is_empty else outer.difference(inner)
            band = make_valid(band.intersection(tile_box))
        except Exception:  # noqa: BLE001
            return None
        return None if band.is_empty else band

    # Medium tiles: 1° / 2.5° metric subcells.
    sub_deg = 1.0 if n > 1500 else (0.0 if n <= 400 else 2.5)
    if sub_deg > 0:
        bands = []
        lat = south
        while lat < north - 1e-9:
            lon = west
            nlat = min(lat + sub_deg, north)
            while lon < east - 1e-9:
                nlon = min(lon + sub_deg, east)
                cell = box(lon, lat, nlon, nlat)
                cell_geoms = []
                for g in clipped:
                    try:
                        inter = g.intersection(cell)
                    except Exception:  # noqa: BLE001
                        continue
                    if not inter.is_empty:
                        cell_geoms.append(inter)
                if cell_geoms:
                    land_u = _chunked_union(cell_geoms)
                    b = _band_from_land(land_u, lon, lat, nlon, nlat, to_metric, to_wgs)
                    if b is not None:
                        bands.append(b)
                lon = nlon
            lat = nlat
        if not bands:
            return None
        try:
            return make_valid(_chunked_union(bands))
        except Exception:  # noqa: BLE001
            return bands[0]

    land_u = _chunked_union(clipped)
    return _band_from_land(land_u, west, south, east, north, to_metric, to_wgs)


def _stream_write_feature_collection(parts_path: Path, out_path: Path) -> None:
    """Assemble FeatureCollection without holding the full JSON string in RAM."""
    with out_path.open("w") as out, parts_path.open() as parts:
        out.write('{"type":"FeatureCollection","features":[')
        first = True
        n = 0
        for line in parts:
            line = line.strip()
            if not line:
                continue
            if not first:
                out.write(",")
            first = False
            out.write(line)
            n += 1
        out.write("]}")
    print(f"[band] wrote {out_path} ({n} tile parts, streamed)", flush=True)


def build_coastal_band(force: bool = False) -> Path:
    if OUT_PATH.exists() and OUT_PATH.stat().st_size > 1000 and not force:
        print(f"[band] already present: {OUT_PATH}")
        return OUT_PATH

    WORK.mkdir(parents=True, exist_ok=True)
    if use_sample():
        print("[band] SAMPLE mode — Isle of Wight / Solent only", flush=True)
    ensure_land_geojsonl()
    # Reuse shards when present (resume / restart after a hung heavy tile).
    if not TILE_DIR.exists() or not any(TILE_DIR.glob("land_*.geojsonl")):
        _shard_land_to_disk()
    else:
        print(f"[band] reusing shards in {TILE_DIR}", flush=True)

    to_metric = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True).transform
    to_wgs = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True).transform

    done: set[tuple[int, int]] = set()
    if PARTS_PATH.exists() and PARTS_PATH.stat().st_size > 100 and "--fresh" not in sys.argv:
        with PARTS_PATH.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    props = json.loads(line).get("properties") or {}
                    done.add((int(props["tx"]), int(props["ty"])))
                except Exception:  # noqa: BLE001
                    continue
        print(f"[band] resuming — {len(done)} tile parts already done", flush=True)
        parts_mode = "a"
    else:
        if PARTS_PATH.exists():
            PARTS_PATH.unlink()
        parts_mode = "w"

    tiles = _iter_tiles()
    n_parts = len(done)
    with PARTS_PATH.open(parts_mode) as parts_fp:
        for i, (tx, ty, west, south, east, north) in enumerate(tiles, start=1):
            if (tx, ty) in done:
                continue
            # Shard already includes features that touch this tile (bbox overlap).
            geoms = _load_shard_geoms(_shard_path(tx, ty))
            if not geoms:
                continue

            print(
                f"[band] tile {i}/{len(tiles)} "
                f"({west:.0f},{south:.0f})–({east:.0f},{north:.0f}) "
                f"n={len(geoms)} …",
                flush=True,
            )
            band = _tile_band(geoms, west, south, east, north, to_metric, to_wgs)
            del geoms
            if band is None:
                continue

            feat = {
                "type": "Feature",
                "properties": {"band_m": BAND_M, "tx": tx, "ty": ty},
                "geometry": mapping(band),
            }
            parts_fp.write(json.dumps(feat, separators=(",", ":")) + "\n")
            parts_fp.flush()
            n_parts += 1
            del band

            if i % 10 == 0 or i == len(tiles):
                print(
                    f"[band] tile {i}/{len(tiles)} parts={n_parts}",
                    flush=True,
                )
                gc.collect()

    if n_parts == 0:
        raise RuntimeError("coastal band produced no geometries")

    _stream_write_feature_collection(PARTS_PATH, OUT_PATH)
    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"[band] done {OUT_PATH} ({size_mb:.1f} MB)", flush=True)
    return OUT_PATH


def load_band_geom():
    """Load coastal band as a shapely geometry (MultiPolygon of tile parts).

    Does not unary_union / buffer the continent — callers should use STRtree
    on exploded parts (see fetch_osm_chart_features.py).
    """
    path = OUT_PATH if OUT_PATH.exists() else build_coastal_band()
    # Prefer streaming parts file when present (avoids one huge Feature).
    src = PARTS_PATH if PARTS_PATH.exists() and PARTS_PATH.stat().st_size > 100 else path
    geoms = []
    if src.suffix == ".geojsonl" or src.name.endswith(".geojsonl"):
        with src.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    g = make_valid(shape(json.loads(line)["geometry"]))
                except Exception:  # noqa: BLE001
                    continue
                if g.is_empty:
                    continue
                if g.geom_type == "Polygon":
                    geoms.append(g)
                elif g.geom_type == "MultiPolygon":
                    geoms.extend(p for p in g.geoms if not p.is_empty)
    else:
        data = json.loads(path.read_text())
        for feat in data.get("features") or []:
            try:
                g = make_valid(shape(feat["geometry"]))
            except Exception:  # noqa: BLE001
                continue
            if g.is_empty:
                continue
            if g.geom_type == "Polygon":
                geoms.append(g)
            elif g.geom_type == "MultiPolygon":
                geoms.extend(p for p in g.geoms if not p.is_empty)
    if not geoms:
        raise RuntimeError(f"empty coastal band: {path}")
    if len(geoms) == 1:
        return geoms[0]
    return MultiPolygon(geoms)


# Sailing charts only need relief / WindNinja near the sea. The band is 20 km
# from the coastline: sea includes inlets whose mouth is within a few km of
# the open ocean (Solent, Aegean gulfs). Lakes farther inland are not coast.
_COAST_PAD_KM = 20.0
_MOUTH_BRIDGE_KM = 3.0
_KM_PER_DEG_LAT = 111.32
# 0.01° keeps the Solent and island channels as water. 0.05° with all-touched
# land painted those straits as land, so the band never saw their shoreline.
_OCEAN_RES_DEG = 0.01
_OCEAN_MASK = WORK / "ocean_mask_001.npz"
_LAND_CHART = ROOT / "data" / "geo" / "osm" / "land_chart.geojson"
_ocean_cache: dict | None = None

_OCEAN_SEEDS = (
    (-20.0, 50.0),
    (-15.0, 45.0),
    (-8.0, 32.0),
    (0.0, 45.0),
    (5.0, 36.0),
    (12.0, 45.0),
    (16.0, 42.0),
    (18.0, 35.0),
    (25.0, 37.0),
    (28.0, 33.0),
    (3.0, 56.0),
    (20.0, 58.0),
    (32.0, 43.0),
    (-4.0, 50.0),
)


def _raster_shape() -> tuple[int, int, float, float, float]:
    lon0 = float(REGION_BBOX["lon_min"])
    lat0 = float(REGION_BBOX["lat_min"])
    lon1 = float(REGION_BBOX["lon_max"])
    lat1 = float(REGION_BBOX["lat_max"])
    res = _OCEAN_RES_DEG
    width = int(round((lon1 - lon0) / res))
    height = int(round((lat1 - lat0) / res))
    return height, width, lon0, lat1, res


def _rasterize_land_chart(height: int, width: int, lon0: float, lat1: float, res: float):
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415

    if not _LAND_CHART.exists():
        raise FileNotFoundError(f"land chart missing: {_LAND_CHART}")
    gdal_rasterize = shutil.which("gdal_rasterize")
    if not gdal_rasterize:
        raise FileNotFoundError("gdal_rasterize not on PATH")
    WORK.mkdir(parents=True, exist_ok=True)
    tif = WORK / "land_mask_001.tif"
    if tif.exists():
        tif.unlink()
    south = lat1 - height * res
    east = lon0 + width * res
    print(
        f"[band] rasterizing coastline {width}x{height} @{res}° …",
        flush=True,
    )
    subprocess.run(
        [
            gdal_rasterize,
            "-burn",
            "1",
            "-init",
            "0",
            "-ot",
            "Byte",
            "-tr",
            str(res),
            str(res),
            "-te",
            str(lon0),
            str(south),
            str(east),
            str(lat1),
            str(_LAND_CHART),
            str(tif),
        ],
        check=True,
    )
    with rasterio.open(tif) as ds:
        band = ds.read(1)
    tif.unlink(missing_ok=True)
    if band.shape != (height, width):
        raise RuntimeError(f"land raster shape {band.shape} != {(height, width)}")
    return (band > 0)


def _ocean_from_seeds(water, labeled, lon0: float, lat1: float, res: float):
    import numpy as np  # noqa: PLC0415

    height, width = water.shape
    ids: set[int] = set()

    def _take(lon: float, lat: float) -> None:
        c = int((lon - lon0) / res)
        r = int((lat1 - lat) / res)
        if 0 <= r < height and 0 <= c < width and water[r, c]:
            ids.add(int(labeled[r, c]))

    for lon, lat in _OCEAN_SEEDS:
        _take(lon, lat)
    for r in range(height):
        if water[r, 0]:
            ids.add(int(labeled[r, 0]))
    ids.discard(0)
    if not ids:
        return np.zeros_like(water, dtype=bool)
    return np.isin(labeled, np.fromiter(ids, dtype=np.int32))


def _bridge_inlets(water, ocean, labeled, lat1: float, res: float, km: float):
    """Join water bodies whose shore is within ``km`` of the open sea.

    A coarse land pixel can seal the Solent or a gulf. Any cell of that
    water body close to the sea pulls the whole inlet in, so the 20 km
    band follows its coastline. A lake farther inland stays a lake.
    """
    import numpy as np  # noqa: PLC0415
    from scipy.ndimage import distance_transform_edt  # noqa: PLC0415

    height = water.shape[0]
    lat_south = lat1 - height * res
    dlat = res * _KM_PER_DEG_LAT
    dlon = res * _KM_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat_south)))
    dist_km = distance_transform_edt(~ocean, sampling=(dlat, dlon))
    near = water & ~ocean & (dist_km <= km)
    if not np.any(near):
        return ocean
    ids = np.unique(labeled[near])
    ids = ids[ids > 0]
    if ids.size == 0:
        return ocean
    print(f"[band] bridged {ids.size} coastal inlet(s) within {km:.0f} km", flush=True)
    return ocean | np.isin(labeled, ids)


def _build_ocean_mask() -> dict:
    import numpy as np  # noqa: PLC0415
    from scipy.ndimage import label as cc_label  # noqa: PLC0415

    height, width, lon0, lat1, res = _raster_shape()
    land = np.asarray(_rasterize_land_chart(height, width, lon0, lat1, res), dtype=bool)
    water = ~land
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=int)
    print("[band] labelling water bodies …", flush=True)
    labeled, n_water = cc_label(water, structure=structure)
    print(f"[band] water bodies={n_water}", flush=True)
    ocean = _ocean_from_seeds(water, labeled, lon0, lat1, res)
    ocean = _bridge_inlets(water, ocean, labeled, lat1, res, _MOUTH_BRIDGE_KM)

    print(
        f"[band] ocean cells={int(ocean.sum())} land cells={int(land.sum())}",
        flush=True,
    )
    WORK.mkdir(parents=True, exist_ok=True)
    idx = {
        "land": land.astype(bool),
        "ocean": ocean,
        "lon0": lon0,
        "lat1": lat1,
        "res": res,
        "keep": _build_keep(land.astype(bool), ocean, lat1, res),
    }
    _persist_ocean_index(idx)
    print(f"[band] wrote {_OCEAN_MASK}", flush=True)
    return idx


def _ocean_index() -> dict | None:
    global _ocean_cache
    if _ocean_cache is not None:
        return _ocean_cache
    import numpy as np  # noqa: PLC0415

    try:
        if _OCEAN_MASK.exists():
            data = np.load(_OCEAN_MASK)
            _ocean_cache = {
                "land": np.asarray(data["land"], dtype=bool),
                "ocean": np.asarray(data["ocean"], dtype=bool),
                "lon0": float(data["lon0"]),
                "lat1": float(data["lat1"]),
                "res": float(data["res"]),
                "keep": (
                    np.asarray(data["keep"], dtype=bool)
                    if "keep" in data.files
                    else None
                ),
            }
            print("[band] loaded ocean mask", _OCEAN_MASK.name, flush=True)
            return _ocean_cache
        _ocean_cache = _build_ocean_mask()
        return _ocean_cache
    except Exception as exc:  # noqa: BLE001
        print(f"[band] ocean mask failed ({exc}) — not skipping inland", flush=True)
        return None


def _persist_ocean_index(idx: dict) -> None:
    import numpy as np  # noqa: PLC0415

    WORK.mkdir(parents=True, exist_ok=True)
    payload = {
        "land": np.asarray(idx["land"], dtype=np.uint8),
        "ocean": np.asarray(idx["ocean"], dtype=np.uint8),
        "lon0": np.float64(idx["lon0"]),
        "lat1": np.float64(idx["lat1"]),
        "res": np.float64(idx["res"]),
    }
    if idx.get("keep") is not None:
        payload["keep"] = np.asarray(idx["keep"], dtype=np.uint8)
    np.savez_compressed(_OCEAN_MASK, **payload)


def _build_keep(
    land: object,
    ocean: object,
    lat1: float,
    res: float,
    km: float = _COAST_PAD_KM,
):
    """Land within ``km`` of ocean, plus ocean within ``km`` of land."""
    import numpy as np  # noqa: PLC0415
    from scipy.ndimage import distance_transform_edt  # noqa: PLC0415

    land_b = np.asarray(land, dtype=bool)
    ocean_b = np.asarray(ocean, dtype=bool)
    h, _w = land_b.shape
    keep = np.zeros((h, land_b.shape[1]), dtype=bool)
    dlat_km = res * _KM_PER_DEG_LAT
    band_rows = max(1, int(round(5.0 / res)))
    pad_rows = int(math.ceil(km / dlat_km)) + 4
    print(f"[band] computing {km:.0f} km coastal keep strip …", flush=True)
    for r0 in range(0, h, band_rows):
        r1 = min(h, r0 + band_rows)
        sl0 = max(0, r0 - pad_rows)
        sl1 = min(h, r1 + pad_rows)
        mid = 0.5 * (r0 + r1)
        lat = lat1 - (mid + 0.5) * res
        dlon_km = res * _KM_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat)))
        ocean_s = ocean_b[sl0:sl1]
        land_s = land_b[sl0:sl1]
        dist_o = distance_transform_edt(~ocean_s, sampling=(dlat_km, dlon_km))
        dist_l = distance_transform_edt(~land_s, sampling=(dlat_km, dlon_km))
        loc0 = r0 - sl0
        loc1 = loc0 + (r1 - r0)
        near_o = dist_o[loc0:loc1] <= km
        near_l = dist_l[loc0:loc1] <= km
        keep[r0:r1] = (land_b[r0:r1] & near_o) | (ocean_b[r0:r1] & near_l)
    n = int(keep.sum())
    print(f"[band] coastal keep cells={n} / {keep.size} ({100.0 * n / keep.size:.1f}%)", flush=True)
    _write_keep_preview(keep, land_b, ocean_b)
    return keep


def _write_keep_preview(keep, land, ocean) -> None:
    try:
        from PIL import Image  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    rgb = np.zeros((keep.shape[0], keep.shape[1], 3), dtype=np.uint8)
    rgb[:] = (18, 18, 22)
    rgb[np.asarray(ocean, dtype=bool)] = (24, 40, 70)
    rgb[np.asarray(land, dtype=bool)] = (70, 70, 70)
    rgb[np.asarray(keep, dtype=bool)] = (46, 196, 92)
    dest = WORK / "coastal_strip_20km.png"
    Image.fromarray(rgb, "RGB").save(dest)
    print(f"[band] wrote strip preview {dest}", flush=True)


def _ensure_keep(idx: dict):
    keep = idx.get("keep")
    if keep is not None:
        return keep
    keep = _build_keep(idx["land"], idx["ocean"], idx["lat1"], idx["res"])
    idx["keep"] = keep
    try:
        _persist_ocean_index(idx)
    except Exception as exc:  # noqa: BLE001
        print(f"[band] keep cache write failed ({exc})", flush=True)
    return keep


def _mask_window(idx: dict, bb: dict[str, float]):
    import numpy as np  # noqa: PLC0415

    lon0 = idx["lon0"]
    lat1 = idx["lat1"]
    res = idx["res"]
    h, w = idx["land"].shape
    c0 = max(0, int(np.floor((float(bb["lon_min"]) - lon0) / res)))
    c1 = min(w, int(np.ceil((float(bb["lon_max"]) - lon0) / res)))
    r0 = max(0, int(np.floor((lat1 - float(bb["lat_max"])) / res)))
    r1 = min(h, int(np.ceil((lat1 - float(bb["lat_min"])) / res)))
    if c1 <= c0 or r1 <= r0:
        return None
    return r0, r1, c0, c1


def crop_keep_slices(keep, *, pad: int = 1):
    """Row/col slices covering True cells, or None if empty."""
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(keep, dtype=bool)
    rows = np.any(arr, axis=1)
    cols = np.any(arr, axis=0)
    if not np.any(rows) or not np.any(cols):
        return None
    r_idx = np.flatnonzero(rows)
    c_idx = np.flatnonzero(cols)
    r0 = max(0, int(r_idx[0]) - pad)
    r1 = min(arr.shape[0], int(r_idx[-1]) + 1 + pad)
    c0 = max(0, int(c_idx[0]) - pad)
    c1 = min(arr.shape[1], int(c_idx[-1]) + 1 + pad)
    if r1 - r0 < 2 or c1 - c0 < 2:
        return None
    return slice(r0, r1), slice(c0, c1)


def lonlat_in_coastal_keep(lon: float, lat: float) -> bool:
    """True when the point is in the 20 km coastal strip.

    If the ocean mask cannot be built, returns True so coast is not dropped.
    """
    idx = _ocean_index()
    if idx is None:
        return True
    keep = _ensure_keep(idx)
    lon0 = idx["lon0"]
    lat1 = idx["lat1"]
    res = idx["res"]
    h, w = keep.shape
    c = int(math.floor((float(lon) - lon0) / res))
    r = int(math.floor((lat1 - float(lat)) / res))
    if r < 0 or r >= h or c < 0 or c >= w:
        return False
    return bool(keep[r, c])


def coastal_keep_on_lonlat(lons, lats):
    """Boolean keep on an ascending lat/lon lattice, or None if mask missing."""
    import numpy as np  # noqa: PLC0415

    idx = _ocean_index()
    if idx is None:
        return None
    keep = _ensure_keep(idx)
    lon0 = idx["lon0"]
    lat1 = idx["lat1"]
    res = idx["res"]
    h, w = keep.shape
    cols = np.floor((np.asarray(lons, dtype=float) - lon0) / res).astype(int)
    rows = np.floor((lat1 - np.asarray(lats, dtype=float)) / res).astype(int)
    cols = np.clip(cols, 0, w - 1)
    rows = np.clip(rows, 0, h - 1)
    return keep[rows[:, None], cols[None, :]]


def coastal_keep_on_grid(shape: tuple[int, int], transform, crs):
    """Float32 0/1 keep resampled onto ``shape`` / ``transform`` / ``crs``."""
    import numpy as np  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from rasterio.warp import Resampling, reproject  # noqa: PLC0415

    idx = _ocean_index()
    if idx is None:
        return np.ones(shape, dtype=np.float32)
    keep = _ensure_keep(idx)
    dest = np.zeros(shape, dtype=np.float32)
    src_transform = from_origin(idx["lon0"], idx["lat1"], idx["res"], idx["res"])
    reproject(
        source=keep.astype(np.float32),
        destination=dest,
        src_transform=src_transform,
        src_crs="EPSG:4326",
        dst_transform=transform,
        dst_crs=crs,
        resampling=Resampling.nearest,
    )
    return (dest >= 0.5).astype(np.float32)


def land_on_grid(shape: tuple[int, int], transform, crs):
    """Float32 0/1 land from the coastline mask, or None if that mask is missing.

    Hillshade must stay off the sea. The OSM land clip is optional; this raster
    is the same chart coastline used for the 20 km strip.
    """
    import numpy as np  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from rasterio.warp import Resampling, reproject  # noqa: PLC0415

    idx = _ocean_index()
    land = None if idx is None else idx.get("land")
    if land is None:
        return None
    dest = np.zeros(shape, dtype=np.float32)
    src_transform = from_origin(idx["lon0"], idx["lat1"], idx["res"], idx["res"])
    reproject(
        source=np.asarray(land, dtype=np.float32),
        destination=dest,
        src_transform=src_transform,
        src_crs="EPSG:4326",
        dst_transform=transform,
        dst_crs=crs,
        resampling=Resampling.nearest,
    )
    return (dest >= 0.5).astype(np.float32)


def bbox_near_coast(
    bb: dict[str, float],
    *,
    pad_km: float = _COAST_PAD_KM,
    pad_deg: float | None = None,
) -> bool:
    """True when the bbox overlaps the 20 km sea-coast strip.

    Lakes do not count as ocean. Used to skip inland DEM / WindNinja work.
    If the mask cannot be built, returns True so coast is not dropped.
    """
    del pad_km, pad_deg
    idx = _ocean_index()
    if idx is None:
        return True
    keep = _ensure_keep(idx)
    win = _mask_window(idx, bb)
    if win is None:
        return False
    r0, r1, c0, c1 = win
    return bool(keep[r0:r1, c0:c1].any())


def main() -> int:
    force = "--force" in sys.argv
    try:
        build_coastal_band(force=force)
    except Exception as exc:  # noqa: BLE001
        print(f"[band] failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
