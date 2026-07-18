#!/usr/bin/env python3
"""Build data/geo/osm/land_chart.geojson — OSM land for the routing LandMask.

Same source polygons as coastline.pmtiles (OSM land-polygons-split). Clips to
the active chart bbox (full REGION_BBOX or Solent --sample).

Requires: ogr2ogr on PATH.
  python services/api/scripts/fetch_osm_land.py
  python services/api/scripts/build_route_land.py [--sample]
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
OUT = ROOT / "data" / "geo" / "osm" / "land_chart.geojson"
WORK = ROOT / "data" / "cache" / "tiles_build"
LAND_GEOJSONL = WORK / "land.geojsonl"


def _find_osm_shp() -> Path | None:
    for p in OSM_SHP_CANDIDATES:
        if p.exists():
            return p
    matches = sorted(
        (ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"),
        key=lambda p: (0 if "split" in str(p) else 1, str(p)),
    )
    return matches[0] if matches else None


def _from_geojsonl(src: Path, dst: Path) -> int:
    """Convert existing clipped land.geojsonl into a FeatureCollection."""
    features: list[dict] = []
    with src.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            feat = json.loads(line)
            feat.setdefault("properties", {})
            features.append(feat)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    return len(features)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    bb = chart_bbox()
    if use_sample():
        print("[route-land] SAMPLE mode — Solent clip")

    # Prefer reusing the coastline build's already-clipped land.geojsonl when
    # it matches the active bbox (same ogr2ogr clip).
    if LAND_GEOJSONL.exists() and LAND_GEOJSONL.stat().st_size > 1000:
        print(f"[route-land] converting {LAND_GEOJSONL} → {OUT}")
        n = _from_geojsonl(LAND_GEOJSONL, OUT)
        print(f"[route-land] wrote {n} polygons → {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
        return 0

    shp = _find_osm_shp()
    if shp is None:
        print(
            "[route-land] OSM land polygons missing.\n"
            "       Run: python services/api/scripts/fetch_osm_land.py",
            file=sys.stderr,
        )
        return 1

    if OUT.exists():
        OUT.unlink()
    clip = f"{bb['lon_min']} {bb['lat_min']} {bb['lon_max']} {bb['lat_max']}"
    cmd = [
        "ogr2ogr",
        "-f",
        "GeoJSON",
        "-clipsrc",
        *clip.split(),
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "COORDINATE_PRECISION=7",
        str(OUT),
        str(shp),
    ]
    print("[route-land] ogr2ogr:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"[route-land] wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
