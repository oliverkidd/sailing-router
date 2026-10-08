#!/usr/bin/env python3
"""Bake Terrain-RGB tiles the 20 km coastline now includes but the disk lacks.

The old ocean mask treated narrow seas (Solent, some Aegean gulfs) as land, so
zoom 12+ tiles along those shores were never kept. This fills only the missing
PNGs. Tiles already on disk are left as they are.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_terrain_rgb import (  # noqa: E402
    DEM_DIR,
    OUT_DIR,
    SHADE_OUT_DIR,
    WORK,
    _bake_dem_bbox,
    _cog_bbox,
)
from coastal_band import lonlat_in_coastal_keep  # noqa: E402

# Same union as apps/web/src/lib/coverageBoxes.ts / prune_keep_tiles.py.
BOXES: list[tuple[float, float, float, float]] = [
    (-10.8, 48.2, 1.6, 52.2),
    (-9.8, 35.9, 8.4, 51.2),
    (-5.8, 33.9, 20.0, 45.9),
    (19.5, 33.9, 36.4, 41.2),
]
# Scrub starts at z12. Sample fine enough to hit a missing z16 tile.
_SAMPLE_DEG = 0.02
_ZOOMS = (12, 14, 16)


def _in_boxes(lon: float, lat: float) -> bool:
    for lon_min, lat_min, lon_max, lat_max in BOXES:
        if lon_min <= lon <= lon_max and lat_min <= lat <= lat_max:
            return True
    return False


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def _tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return west, south, east, north


def _missing_bbox(cog_bb: dict[str, float]) -> dict[str, float] | None:
    seen: set[tuple[int, int, int]] = set()
    west = south = math.inf
    east = north = -math.inf
    found = False
    lat = cog_bb["lat_min"] + _SAMPLE_DEG / 2
    while lat < cog_bb["lat_max"]:
        lon = cog_bb["lon_min"] + _SAMPLE_DEG / 2
        while lon < cog_bb["lon_max"]:
            if _in_boxes(lon, lat) and lonlat_in_coastal_keep(lon, lat):
                for z in _ZOOMS:
                    x, y = _tile_xy(lon, lat, z)
                    key = (z, x, y)
                    if key in seen:
                        continue
                    seen.add(key)
                    rgb = OUT_DIR / str(z) / str(x) / f"{y}.png"
                    shade = SHADE_OUT_DIR / str(z) / str(x) / f"{y}.png"
                    if rgb.exists() and shade.exists():
                        continue
                    tw, ts, te, tn = _tile_bounds(z, x, y)
                    west, south = min(west, tw), min(south, ts)
                    east, north = max(east, te), max(north, tn)
                    found = True
            lon += _SAMPLE_DEG
        lat += _SAMPLE_DEG
    if not found:
        return None
    return {
        "lon_min": max(cog_bb["lon_min"], west - 0.01),
        "lat_min": max(cog_bb["lat_min"], south - 0.01),
        "lon_max": min(cog_bb["lon_max"], east + 0.01),
        "lat_max": min(cog_bb["lat_max"], north + 0.01),
    }


def main() -> None:
    cogs = sorted(DEM_DIR.glob("Copernicus_DSM_*.tif"))
    jobs: list[tuple[Path, dict[str, float]]] = []
    for path in cogs:
        if path.stat().st_size < 100_000:
            continue
        bb = _cog_bbox(path)
        if bb is None:
            continue
        if not any(
            not (bb["lon_max"] < lon_min or bb["lon_min"] > lon_max or bb["lat_max"] < lat_min or bb["lat_min"] > lat_max)
            for lon_min, lat_min, lon_max, lat_max in BOXES
        ):
            continue
        gap = _missing_bbox(bb)
        if gap is None:
            continue
        jobs.append((path, gap))
        print(f"[rebake] gap {path.name} {gap['lon_min']:.3f},{gap['lat_min']:.3f} {gap['lon_max']:.3f},{gap['lat_max']:.3f}", flush=True)

    def _priority(item: tuple[Path, dict[str, float]]) -> tuple[int, str]:
        _path, gap = item
        clon = (gap["lon_min"] + gap["lon_max"]) / 2
        clat = (gap["lat_min"] + gap["lat_max"]) / 2
        if 50.4 <= clat <= 51.2 and -1.9 <= clon <= -0.8:
            return (0, _path.name)
        if 19.5 <= clon <= 36.4 and 33.9 <= clat <= 41.2:
            return (1, _path.name)
        return (2, _path.name)

    jobs.sort(key=_priority)
    print(f"[rebake] {len(jobs)} degree tile(s) to fill", flush=True)
    for i, (path, gap) in enumerate(jobs, start=1):
        print(f"[rebake] {i}/{len(jobs)} {path.name}", flush=True)
        _bake_dem_bbox(
            path,
            gap,
            shade_only=False,
            merge=True,
            fill_missing=False,
            work=WORK / "gap" / path.stem,
            skip_existing=True,
        )
    print("REBAKE DONE", flush=True)


if __name__ == "__main__":
    main()
