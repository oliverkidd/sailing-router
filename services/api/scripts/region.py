"""Shared UK+Europe chart region for tile builds and app maxBounds."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# lon -25..42, lat 30..72 — UK, Ireland, Scandinavia, W Europe, Med.
REGION_BBOX = {
    "lon_min": -25.0,
    "lat_min": 30.0,
    "lon_max": 42.0,
    "lat_max": 72.0,
}

# Isle of Wight / Solent — style preview before full-region Overpass crawl.
SAMPLE_BBOX = {
    "lon_min": -1.75,
    "lat_min": 50.55,
    "lon_max": -0.95,
    "lat_max": 50.95,
}

# Kea / Kythnos / Syros — WindNinja terrain-response test area.
CYCLADES_BBOX = {
    "lon_min": 24.3,
    "lat_min": 37.2,
    "lon_max": 25.2,
    "lat_max": 38.0,
}

# Isobath levels (metres, positive-down) for depth.pmtiles.
# Includes 0.5 / 1 m for harbour-scale continuity (soft_noclip_05_1m).
DEPTH_LEVELS_M = (0.5, 1, 2, 4, 5, 10, 20, 50, 100, 200)

# EMODnet DTM native resolution ≈ 1/16 arc-minute.
EMODNET_RES_DEG = 1.0 / 960.0


def use_sample() -> bool:
    """True when CHART_SAMPLE=1 or --sample is on the CLI."""
    if os.environ.get("CHART_SAMPLE") == "1":
        return True
    return "--sample" in sys.argv


def use_cyclades() -> bool:
    """True when CHART_CYCLADES=1 or --cyclades is on the CLI."""
    if os.environ.get("CHART_CYCLADES") == "1":
        return True
    return "--cyclades" in sys.argv


def parse_bbox_arg(argv: list[str] | None = None) -> dict[str, float] | None:
    """Parse ``--bbox lon_min,lat_min,lon_max,lat_max`` from argv (or sys.argv)."""
    args = list(sys.argv[1:] if argv is None else argv)
    for i, tok in enumerate(args):
        if tok == "--bbox" and i + 1 < len(args):
            parts = [p.strip() for p in args[i + 1].split(",")]
            if len(parts) != 4:
                raise ValueError(
                    "--bbox expects lon_min,lat_min,lon_max,lat_max (4 comma-separated floats)"
                )
            lon_min, lat_min, lon_max, lat_max = (float(p) for p in parts)
            return {
                "lon_min": lon_min,
                "lat_min": lat_min,
                "lon_max": lon_max,
                "lat_max": lat_max,
            }
        if tok.startswith("--bbox="):
            return parse_bbox_arg(["--bbox", tok.split("=", 1)[1]])
    return None


def chart_bbox() -> dict[str, float]:
    """Active build bbox: --bbox, Cyclades, Solent sample, or full REGION_BBOX."""
    override = parse_bbox_arg()
    if override is not None:
        return override
    if use_cyclades():
        return CYCLADES_BBOX
    if use_sample():
        return SAMPLE_BBOX
    return REGION_BBOX


def archive_output(path: Path) -> Path | None:
    """Move an existing build output into data/geo/archive/ before a rebuild.

    Stashes a datestamped copy of the previous PMTiles instead of deleting it,
    so a rebuild never silently wipes the last good tiles. Returns the archive
    path, or None if there was nothing to archive.
    """
    if not path.exists():
        return None
    root = Path(__file__).resolve().parents[3]
    archive_dir = root / "data" / "geo" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest
