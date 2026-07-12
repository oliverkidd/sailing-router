#!/usr/bin/env python3
"""Download OSM land polygons (sole coastline source) for chart tile builds.

Source: https://osmdata.openstreetmap.de/ — land-polygons-split-4326.zip

We intentionally use the *split* package: complete land polygons yield
continent-scale MultiPolygons (millions of vertices) that tippecanoe cannot
reliably clip into mid-zoom tiles (empty land / navy holes). Split cells are
manageable; tippecanoe --detect-shared-borders hides artificial cell edges so
coastline outlines stay true coasts.

Fails hard if the download or extract is missing / incomplete.
"""

from __future__ import annotations

import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OSM_DIR = ROOT / "data" / "geo" / "osm"
ZIP_PATH = OSM_DIR / "land-polygons-split-4326.zip"
SHP_PATH = OSM_DIR / "land-polygons-split-4326" / "land_polygons.shp"

# WGS84 land polygons pre-split into a grid (required for tippecanoe reliability).
OSM_LAND_URL = "https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip"


def main() -> int:
    OSM_DIR.mkdir(parents=True, exist_ok=True)

    if SHP_PATH.exists() and SHP_PATH.stat().st_size > 1_000_000:
        print(f"[osm] already present: {SHP_PATH}")
        return 0

    print(f"[osm] downloading {OSM_LAND_URL}")
    print(f"[osm] → {ZIP_PATH} (≈900 MB; this can take a while)")
    try:
        urllib.request.urlretrieve(OSM_LAND_URL, ZIP_PATH)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[osm] FAILED to download OSM land polygons: {exc}\n"
            f"      Manual: curl -L -o {ZIP_PATH} {OSM_LAND_URL}",
            file=sys.stderr,
        )
        return 1

    if not ZIP_PATH.exists() or ZIP_PATH.stat().st_size < 10_000_000:
        print("[osm] download incomplete or too small — abort", file=sys.stderr)
        return 1

    print(f"[osm] extracting {ZIP_PATH} …")
    try:
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            zf.extractall(OSM_DIR)
    except Exception as exc:  # noqa: BLE001
        print(f"[osm] FAILED to extract: {exc}", file=sys.stderr)
        return 1

    if not SHP_PATH.exists():
        matches = sorted(
            (ROOT / "data" / "geo" / "osm").rglob("land_polygons.shp"),
            key=lambda p: (0 if "split" in str(p) else 1, str(p)),
        )
        if not matches:
            print("[osm] land_polygons.shp missing after extract — abort", file=sys.stderr)
            return 1
        print(f"[osm] found shapefile at {matches[0]}")
    else:
        print(f"[osm] ready: {SHP_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
