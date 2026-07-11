#!/usr/bin/env python3
"""Download OSM land polygons (sole coastline source) for chart tile builds.

Source: https://osmdata.openstreetmap.de/ — land-polygons-split-4326.zip
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

# Documented download URL (WGS84, large polygons split for rendering).
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
        # Some archives nest differently — search.
        matches = list(OSM_DIR.rglob("land_polygons.shp"))
        if not matches:
            print("[osm] land_polygons.shp missing after extract — abort", file=sys.stderr)
            return 1
        print(f"[osm] found shapefile at {matches[0]}")
    else:
        print(f"[osm] ready: {SHP_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
