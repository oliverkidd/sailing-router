#!/usr/bin/env python3
"""Build coastline.pmtiles for the UK+Europe chart region.

Layers:
  land   — OSM land polygons (split, tippecanoe-friendly)
  water  — OSM coastal water polygons (estuaries / riverbanks / harbours)
  places — OSM coastal places (city/town/island/harbour)
  roads  — OSM major highways (subtle underlay)
  rivers — OSM named rivers/canals (subtle underlay + line labels)

Requires: tippecanoe, ogr2ogr on PATH.
  python scripts/fetch_osm_land.py
  python scripts/fetch_osm_chart_features.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import archive_output, chart_bbox, use_sample  # noqa: E402

OSM_SHP_CANDIDATES = [
    ROOT / "data" / "geo" / "osm" / "land-polygons-split-4326" / "land_polygons.shp",
    ROOT / "data" / "geo" / "osm" / "land_polygons.shp",
]
WORK = ROOT / "data" / "cache" / "tiles_build"
OUT = ROOT / "apps" / "web" / "public" / "geo" / "coastline.pmtiles"

OSM_PLACES = WORK / "osm_places.geojsonl"
OSM_ROADS = WORK / "osm_roads.geojsonl"
OSM_RIVERS = WORK / "osm_rivers.geojsonl"
OSM_WATER = WORK / "osm_water.geojsonl"
COASTLINE = WORK / "coastline.geojsonl"
LAND_SEAMS = WORK / "land_seams.geojsonl"


def _find_osm_shp() -> Path | None:
    for p in OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = sorted(
        (ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"),
        key=lambda p: (0 if "split" in str(p) else 1, str(p)),
    )
    return matches[0] if matches else None


def _write_land_geojsonl(shp: Path, path: Path) -> int:
    """Clip OSM land to active chart bbox with ogr2ogr — no geometry simplify."""
    if path.exists():
        path.unlink()

    bb = chart_bbox()
    clip = (
        f"{bb['lon_min']} {bb['lat_min']} "
        f"{bb['lon_max']} {bb['lat_max']}"
    )
    cmd = [
        "ogr2ogr",
        "-f",
        "GeoJSONSeq",
        "-clipsrc",
        *clip.split(),
        "-nlt",
        "PROMOTE_TO_MULTI",
        str(path),
        str(shp),
    ]
    print("[coast] ogr2ogr clip:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    n = 0
    tmp = path.with_suffix(".tmp.geojsonl")
    with path.open() as inp, tmp.open("w") as out:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            feat["properties"] = {}
            out.write(json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
    tmp.replace(path)
    return n


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open() as fh:
        for _ in fh:
            n += 1
    return n


def _orient_water_geojsonl(path: Path) -> int:
    """Force GeoJSON exterior rings CCW so fills survive tippecanoe.

    OSM/shapely often emit clockwise outers. With wrong winding (or
    --use-source-polygon-winding) tippecanoe treats them as holes and the
    water layer vanishes from tiles.
    """
    if not path.exists() or path.stat().st_size == 0:
        return 0
    from shapely.geometry import mapping, shape
    from shapely.ops import orient

    tmp = path.with_suffix(".orient.tmp.geojsonl")
    n = 0
    with path.open() as inp, tmp.open("w") as out:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            try:
                geom = orient(shape(feat["geometry"]), sign=1.0)
                feat["geometry"] = mapping(geom)
            except Exception:  # noqa: BLE001
                pass
            out.write(json.dumps(feat, separators=(",", ":")) + "\n")
            n += 1
    tmp.replace(path)
    return n


# Coastline extraction: quantise coords to ~1e-7 deg integer point-ids so the
# grid cuts introduced by land-polygons-split (coincident in both neighbours)
# collapse to identical segments. lon >= -25 so a +2.5e8 offset keeps ids >= 0.
_COAST_SCALE = 1.0e7
_COAST_LON_OFF = 250_000_000
_COAST_MULT = 1_000_000_000


def _extract_coastline(land_path: Path, out_path: Path) -> int:
    """Write a clean coastline line layer from the land polygons.

    Each boundary segment shared by two split-polygons (a grid cut inside a
    landmass) appears exactly twice and is dropped; real coast / lake edges
    appear once and are kept. Consecutive kept segments in a ring are merged
    into a single LineString, so the outline hugs the land fill with no
    internal split-grid seams.
    """
    import numpy as np  # noqa: PLC0415

    def to_pids(coords: list) -> "np.ndarray":
        arr = np.asarray(coords, dtype=np.float64)
        qx = np.round(arr[:, 0] * _COAST_SCALE).astype(np.int64) + _COAST_LON_OFF
        qy = np.round(arr[:, 1] * _COAST_SCALE).astype(np.int64)
        return qx * _COAST_MULT + qy

    rings: list[np.ndarray] = []
    seg_lens: list[int] = []
    seg_arrays: list[np.ndarray] = []
    with land_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            geom = json.loads(line).get("geometry") or {}
            gtype = geom.get("type")
            if gtype == "MultiPolygon":
                polys = geom["coordinates"]
            elif gtype == "Polygon":
                polys = [geom["coordinates"]]
            else:
                continue
            for poly in polys:
                for ring in poly:
                    if len(ring) < 2:
                        continue
                    pids = to_pids(ring)
                    rings.append(pids)
                    a = pids[:-1]
                    b = pids[1:]
                    lo = np.minimum(a, b)
                    hi = np.maximum(a, b)
                    seg_arrays.append(np.stack([lo, hi], axis=1))
                    seg_lens.append(len(pids) - 1)

    if not seg_arrays:
        out_path.write_text("")
        return 0

    all_segs = np.concatenate(seg_arrays, axis=0)
    seg_arrays.clear()
    _uniq, inverse, counts = np.unique(
        all_segs, axis=0, return_inverse=True, return_counts=True
    )
    inverse = np.asarray(inverse).ravel()
    kept = counts[inverse] == 1  # per-segment, in original ring order
    del all_segs, inverse, counts

    n = 0
    base = 0
    tmp = out_path.with_suffix(".tmp.geojsonl")
    with tmp.open("w") as out:
        for pids, m in zip(rings, seg_lens):
            mask = kept[base : base + m]
            base += m
            j = 0
            while j < m:
                if not mask[j]:
                    j += 1
                    continue
                start = j
                while j < m and mask[j]:
                    j += 1
                pts = pids[start : j + 1]
                lon = (pts // _COAST_MULT - _COAST_LON_OFF) / _COAST_SCALE
                lat = (pts % _COAST_MULT) / _COAST_SCALE
                coords = [
                    [round(float(x), 7), round(float(y), 7)]
                    for x, y in zip(lon.tolist(), lat.tolist())
                ]
                feat = {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
                out.write(json.dumps(feat, separators=(",", ":")) + "\n")
                n += 1
    tmp.replace(out_path)
    return n


# OSM land-polygons-split insets each cell ~0.0007° off the integer grid, so
# neighbour edges are not coincident and the shared-segment pass above keeps
# them. Drop those cuts here: axis-aligned and on (or ~1° long near) integers.
_GRID_NEAR_INT = 0.002
_GRID_AXIS_EPS = 1.5e-4
_GRID_MIN_LEN = 0.08
_GRID_LONG_LEN = 0.45


def _near_integer_deg(v: float) -> bool:
    return abs(v - round(v)) < _GRID_NEAR_INT


def _is_split_grid_seg(a: list[float], b: list[float]) -> bool:
    dx = abs(b[0] - a[0])
    dy = abs(b[1] - a[1])
    length = (dx * dx + dy * dy) ** 0.5
    if length < _GRID_MIN_LEN:
        return False
    vertical = dx < _GRID_AXIS_EPS
    horizontal = dy < _GRID_AXIS_EPS
    if not vertical and not horizontal:
        return False
    if vertical and _near_integer_deg(a[0]) and _near_integer_deg(b[0]):
        return True
    if horizontal and _near_integer_deg(a[1]) and _near_integer_deg(b[1]):
        return True
    return length >= _GRID_LONG_LEN


def _write_linestring(out, coords: list) -> None:
    if len(coords) < 2:
        return
    feat = {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "LineString", "coordinates": coords},
    }
    out.write(json.dumps(feat, separators=(",", ":")) + "\n")


def _partition_split_grid(src: Path, coast_out: Path, seams_out: Path) -> tuple[int, int]:
    """Split coastline lines into true shore vs OSM split-grid cuts."""
    n_coast = 0
    n_seams = 0
    coast_tmp = coast_out.with_suffix(".part.tmp.geojsonl")
    seams_tmp = seams_out.with_suffix(".part.tmp.geojsonl")
    with src.open() as inp, coast_tmp.open("w") as coast_fh, seams_tmp.open("w") as seam_fh:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            geom = json.loads(line).get("geometry") or {}
            gtype = geom.get("type")
            if gtype == "LineString":
                rings = [geom.get("coordinates") or []]
            elif gtype == "MultiLineString":
                rings = geom.get("coordinates") or []
            else:
                continue
            for coords in rings:
                if len(coords) < 2:
                    continue
                grid = _is_split_grid_seg(coords[0], coords[1])
                run = [coords[0], coords[1]]
                for i in range(2, len(coords)):
                    nxt = _is_split_grid_seg(coords[i - 1], coords[i])
                    if nxt == grid:
                        run.append(coords[i])
                        continue
                    if grid:
                        _write_linestring(seam_fh, run)
                        n_seams += 1
                    else:
                        _write_linestring(coast_fh, run)
                        n_coast += 1
                    grid = nxt
                    run = [coords[i - 1], coords[i]]
                if grid:
                    _write_linestring(seam_fh, run)
                    n_seams += 1
                else:
                    _write_linestring(coast_fh, run)
                    n_coast += 1
    # Allow in-place replace of src.
    coast_tmp.replace(coast_out)
    seams_tmp.replace(seams_out)
    return n_coast, n_seams


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    if use_sample():
        print("[coast] SAMPLE mode — Isle of Wight / Solent land + features")

    shp = _find_osm_shp()
    if shp is None:
        print(
            "[coast] OSM land polygons missing.\n"
            "       Run: python services/api/scripts/fetch_osm_land.py",
            file=sys.stderr,
        )
        return 1

    min_places_bytes = 200 if use_sample() else 1000
    if not OSM_PLACES.exists() or OSM_PLACES.stat().st_size < min_places_bytes:
        print(
            "[coast] OSM chart features missing (places/roads/rivers/water).\n"
            "       Run: python services/api/scripts/fetch_osm_chart_features.py [--sample]",
            file=sys.stderr,
        )
        return 1

    land_path = WORK / "land.geojsonl"

    # Sample always reclips; full region reuses a large land.geojsonl when present.
    reuse_ok = (
        not use_sample()
        and land_path.exists()
        and land_path.stat().st_size > 1_000_000
    )
    if reuse_ok:
        n_land = _count_lines(land_path)
        print(f"[coast] reusing land.geojsonl ({n_land} polygons)")
    else:
        bb = chart_bbox()
        print(
            f"[coast] clipping OSM land ({shp}) to "
            f"({bb['lon_min']},{bb['lat_min']})–({bb['lon_max']},{bb['lat_max']}) …"
        )
        try:
            n_land = _write_land_geojsonl(shp, land_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[coast] OSM clip failed: {exc}", file=sys.stderr)
            return 1
        print(f"[coast] wrote {n_land} land polygons → {land_path}")

    n_places = _count_lines(OSM_PLACES)
    n_roads = _count_lines(OSM_ROADS)
    n_rivers = _count_lines(OSM_RIVERS)
    if not OSM_WATER.exists():
        OSM_WATER.write_text("")
    n_water = _orient_water_geojsonl(OSM_WATER)
    print(
        f"[coast] OSM places={n_places} roads={n_roads} rivers={n_rivers} water={n_water}"
    )

    if n_land == 0 or n_places == 0:
        print("[coast] missing land or places — abort", file=sys.stderr)
        return 1

    print("[coast] extracting coastline outline (drop split-grid seams) …", flush=True)
    n_coast = _extract_coastline(land_path, COASTLINE)
    print(f"[coast] wrote {n_coast} coastline lines → {COASTLINE}")

    print("[coast] partitioning integer-grid cuts → land_seams …", flush=True)
    n_coast, n_seams = _partition_split_grid(COASTLINE, COASTLINE, LAND_SEAMS)
    print(f"[coast] shore lines={n_coast} seam lines={n_seams} → {LAND_SEAMS}")

    archived = archive_output(OUT)
    if archived:
        print(f"[coast] archived previous tiles → {archived}")

    # Solid land fills; do not stroke land edges in MapLibre (split-grid seams).
    # Water polygons paint sea-color on top of land (estuaries / harbour basins).
    cmd = [
        "tippecanoe",
        "-o",
        str(OUT),
        "-Z0",
        "-z14",
        "--force",
        # Respect the ~500 KB tile limit: simplify low-zoom geometry and
        # coalesce/drop the densest features so zoomed-out tiles stay light.
        "--simplification=10",
        "--drop-densest-as-needed",
        "--coalesce-densest-as-needed",
        "--detect-shared-borders",
        "--tiny-polygon-size=0",
        "-L",
        f"land:{land_path}",
        "-L",
        f"coastline:{COASTLINE}",
        "-L",
        f"land_seams:{LAND_SEAMS}",
        "-L",
        f"water:{OSM_WATER}",
        "-L",
        f"places:{OSM_PLACES}",
        "-L",
        f"roads:{OSM_ROADS}",
        "-L",
        f"rivers:{OSM_RIVERS}",
    ]
    print("[coast] tippecanoe:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"[coast] wrote {OUT} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
