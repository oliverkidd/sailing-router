#!/usr/bin/env python3
"""Build coastline.pmtiles (OSM land + NE places) for the UK+Europe chart region.

Sole land source: OpenStreetMap land polygons (osmdata.openstreetmap.de).
No GSHHS / Natural Earth land fallback — missing OSM input exits non-zero.

Requires: tippecanoe, ogr2ogr on PATH.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from shapely.geometry import Point, box

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from region import REGION_BBOX  # noqa: E402

OSM_SHP_CANDIDATES = [
    ROOT / "data" / "geo" / "osm" / "land-polygons-split-4326" / "land_polygons.shp",
    ROOT / "data" / "geo" / "osm" / "land_polygons.shp",
]
NE_PLACES = ROOT / "data" / "geo" / "ne_10m_populated_places.geojson"
NE_GEOGRAPHY = ROOT / "data" / "geo" / "ne_10m_geography_regions_points.geojson"
WORK = ROOT / "data" / "cache" / "tiles_build"
OUT = ROOT / "apps" / "web" / "public" / "geo" / "coastline.pmtiles"

_MAX_TOWN_SCALERANK = 8
_MAX_ISLAND_SCALERANK = 8
_ISLAND_FEATURECLAS = frozenset({"island", "island group", "archipelago"})


def _find_osm_shp() -> Path | None:
    for p in OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = list((ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"))
    return matches[0] if matches else None


def _clean_island_name(name: str) -> str:
    n = name.strip()
    for suffix in (" Island", " Is.", " I.", " Islands"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)].strip()
            break
    return n


def _write_land_geojsonl(shp: Path, path: Path) -> int:
    """Clip OSM land to REGION_BBOX with ogr2ogr — no geometry simplify."""
    if path.exists():
        path.unlink()

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
        "-nlt",
        "PROMOTE_TO_MULTI",
        str(path),
        str(shp),
    ]
    print("[coast] ogr2ogr clip:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    n = 0
    # Strip bulky attributes; tippecanoe only needs geometry for land fill.
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


def _write_places_geojsonl(path: Path, clip) -> int:
    features: list[dict] = []

    if NE_PLACES.exists():
        places = json.loads(NE_PLACES.read_text())
        for f in places.get("features", []):
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not clip.contains(Point(lon, lat)):
                continue
            try:
                rank = int(props.get("SCALERANK", 10))
            except (TypeError, ValueError):
                rank = 10
            if rank > _MAX_TOWN_SCALERANK:
                continue
            name = (
                props.get("NAME")
                or props.get("NAMEASCII")
                or props.get("name")
                or ""
            ).strip()
            if not name:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {"name": name, "kind": "town", "rank": rank},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

    if NE_GEOGRAPHY.exists():
        geo = json.loads(NE_GEOGRAPHY.read_text())
        for f in geo.get("features", []):
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            fclass = str(props.get("featurecla") or props.get("FEATURECLA") or "").lower()
            if fclass not in _ISLAND_FEATURECLAS:
                continue
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not clip.contains(Point(lon, lat)):
                continue
            try:
                rank = int(props.get("scalerank", props.get("SCALERANK", 10)))
            except (TypeError, ValueError):
                rank = 10
            if rank > _MAX_ISLAND_SCALERANK:
                continue
            name = _clean_island_name(
                str(props.get("name") or props.get("NAME") or "").strip()
            )
            if not name:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {"name": name, "kind": "island", "rank": rank},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

    with path.open("w") as fh:
        for feat in features:
            fh.write(json.dumps(feat, separators=(",", ":")) + "\n")
    return len(features)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    shp = _find_osm_shp()
    if shp is None:
        print(
            "[coast] OSM land polygons missing.\n"
            "       Run: python services/api/scripts/fetch_osm_land.py\n"
            "       No GSHHS / Natural Earth land fallback.",
            file=sys.stderr,
        )
        return 1

    clip = box(
        REGION_BBOX["lon_min"],
        REGION_BBOX["lat_min"],
        REGION_BBOX["lon_max"],
        REGION_BBOX["lat_max"],
    )

    land_path = WORK / "land.geojsonl"
    places_path = WORK / "places.geojsonl"

    print(f"[coast] clipping OSM land ({shp}) to REGION_BBOX …")
    try:
        n_land = _write_land_geojsonl(shp, land_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[coast] OSM clip failed: {exc}", file=sys.stderr)
        return 1
    print(f"[coast] wrote {n_land} land polygons → {land_path}")

    print("[coast] filtering Natural Earth places (labels only) …")
    n_places = _write_places_geojsonl(places_path, clip)
    print(f"[coast] wrote {n_places} place labels → {places_path}")

    if n_land == 0:
        print("[coast] no land features — abort", file=sys.stderr)
        return 1

    if OUT.exists():
        OUT.unlink()

    # No densest-drop / coalesce — keep mid-zoom land tiles complete.
    cmd = [
        "tippecanoe",
        "-o",
        str(OUT),
        "-Z0",
        "-z14",
        "--force",
        "--no-tile-size-limit",
        "--no-feature-limit",
        "-L",
        f"land:{land_path}",
        "-L",
        f"places:{places_path}",
    ]
    print("[coast] tippecanoe:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"[coast] wrote {OUT} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
