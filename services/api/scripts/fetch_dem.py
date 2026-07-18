#!/usr/bin/env python3
"""Fetch Copernicus DEM GLO-30 tiles for the chart region into data/dem/.

Uses the public AWS Open Data COGs (no API key):
  https://copernicus-dem-30m.s3.amazonaws.com/

Idempotent: skips when data/dem already has a GeoTIFF > 0.1 MB.

Usage:
  python scripts/fetch_dem.py              # full REGION_BBOX
  python scripts/fetch_dem.py --sample     # Solent SAMPLE_BBOX
  python scripts/fetch_dem.py --cyclades   # Kea/Kythnos/Syros test area
  python scripts/fetch_dem.py --bbox lon_min,lat_min,lon_max,lat_max
"""

from __future__ import annotations

import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
# MasterWind/services/api/scripts → parents[2] = MasterWind/
_ROOT = _SCRIPTS.parents[2]
sys.path.insert(0, str(_SCRIPTS))

from region import chart_bbox, parse_bbox_arg, use_cyclades, use_sample  # noqa: E402

DEM_DIR = _ROOT / "data" / "dem"
BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


def _tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_"
        f"{ew}{abs(lon):03d}_00_DEM"
    )


def _tiles_for_bbox(bb: dict[str, float]) -> list[tuple[int, int]]:
    lat0 = math.floor(bb["lat_min"])
    lat1 = math.floor(bb["lat_max"])
    lon0 = math.floor(bb["lon_min"])
    lon1 = math.floor(bb["lon_max"])
    out: list[tuple[int, int]] = []
    for lat in range(lat0, lat1 + 1):
        for lon in range(lon0, lon1 + 1):
            out.append((lat, lon))
    return out


def _dem_present() -> Path | None:
    if not DEM_DIR.exists():
        return None
    for f in sorted(DEM_DIR.glob("*.tif")) + sorted(DEM_DIR.glob("*.tiff")):
        if f.stat().st_size > 100_000:
            return f
    return None


def _download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        print(f"  ← {url}")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
        return True
    except urllib.error.HTTPError as exc:
        if tmp.exists():
            tmp.unlink()
        if exc.code == 404:
            print(f"  (no tile {exc.code})")
            return False
        raise
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _mosaic_vrt(tiles: list[Path], out_tif: Path) -> None:
    """Build a single GeoTIFF via gdalbuildvrt + gdal_translate when available."""
    import shutil
    import subprocess

    gdalbuildvrt = shutil.which("gdalbuildvrt")
    gdal_translate = shutil.which("gdal_translate")
    if not gdalbuildvrt or not gdal_translate:
        for base in (
            "/Applications/Postgres.app/Contents/Versions/latest/bin",
            "/opt/homebrew/bin",
        ):
            if (Path(base) / "gdalbuildvrt").exists():
                gdalbuildvrt = str(Path(base) / "gdalbuildvrt")
                gdal_translate = str(Path(base) / "gdal_translate")
                break
    if not gdalbuildvrt or not gdal_translate:
        print("[fetch-dem] gdalbuildvrt not found — leaving individual tiles")
        return

    vrt = out_tif.with_suffix(".vrt")
    subprocess.run(
        [gdalbuildvrt, str(vrt), *[str(t) for t in tiles]],
        check=True,
    )
    subprocess.run(
        [
            gdal_translate,
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "TILED=YES",
            str(vrt),
            str(out_tif),
        ],
        check=True,
    )
    vrt.unlink(missing_ok=True)
    print(f"[fetch-dem] mosaicked → {out_tif} ({out_tif.stat().st_size / 1e6:.1f} MB)")


def _mosaic_name() -> str:
    if parse_bbox_arg() is not None:
        return "region_dem_custom.tif"
    if use_cyclades():
        return "region_dem_cyclades.tif"
    if use_sample():
        return "region_dem_sample.tif"
    return "region_dem.tif"


def main() -> int:
    bb = chart_bbox()
    existing = _dem_present()
    if existing and "--force" not in sys.argv:
        print(
            f"[fetch-dem] already present: {existing} "
            f"({existing.stat().st_size / 1e6:.1f} MB)"
        )
        return 0

    tiles = _tiles_for_bbox(bb)
    print(f"[fetch-dem] fetching {len(tiles)} GLO-30 tile(s) for {bb}")
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []
    for lat, lon in tiles:
        name = _tile_name(lat, lon)
        url = f"{BASE}/{name}/{name}.tif"
        dest = DEM_DIR / f"{name}.tif"
        if dest.exists() and dest.stat().st_size > 100_000:
            print(f"  ✓ cached {dest.name}")
            downloaded.append(dest)
            continue
        if _download(url, dest):
            downloaded.append(dest)

    if not downloaded:
        print("[fetch-dem] ERROR: no DEM tiles downloaded", file=sys.stderr)
        return 1

    mosaic = DEM_DIR / _mosaic_name()
    if len(downloaded) == 1:
        if mosaic.exists() or mosaic.is_symlink():
            mosaic.unlink()
        mosaic.symlink_to(downloaded[0].name)
        print(f"[fetch-dem] linked {mosaic.name} → {downloaded[0].name}")
    else:
        _mosaic_vrt(downloaded, mosaic)

    print(f"[fetch-dem] done — {len(downloaded)} tile(s) in {DEM_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
