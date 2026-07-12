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

from region import chart_bbox, use_sample  # noqa: E402

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

    if OUT.exists():
        OUT.unlink()

    # Solid land fills; do not stroke land edges in MapLibre (split-grid seams).
    # Water polygons paint sea-color on top of land (estuaries / harbour basins).
    cmd = [
        "tippecanoe",
        "-o",
        str(OUT),
        "-Z0",
        "-z14",
        "--force",
        "--no-tile-size-limit",
        "--no-feature-limit",
        "--detect-shared-borders",
        "-r1",
        "--tiny-polygon-size=0",
        "-L",
        f"land:{land_path}",
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
