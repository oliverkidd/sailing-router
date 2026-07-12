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

from region import chart_bbox, use_sample  # noqa: E402

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
