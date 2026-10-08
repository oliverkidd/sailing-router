#!/usr/bin/env python3
"""Fetch Copernicus DEM GLO-30 tiles for the chart region into data/dem/.

Uses the public AWS Open Data COGs (no API key):
  https://copernicus-dem-30m.s3.amazonaws.com/

Idempotent: skips when the target mosaic/VRT already exists (unless --force).

Usage:
  python scripts/fetch_dem.py              # full REGION_BBOX → 1° tiles + region_dem.vrt
  python scripts/fetch_dem.py --sample     # Solent SAMPLE_BBOX
  python scripts/fetch_dem.py --cyclades   # Aegean 2×2° clip
  python scripts/fetch_dem.py --bbox lon_min,lat_min,lon_max,lat_max
  python scripts/fetch_dem.py --force      # re-download / rebuild VRT
"""

from __future__ import annotations

import math
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _mosaic_name() -> str:
    if parse_bbox_arg() is not None:
        return "region_dem_custom.tif"
    if use_cyclades():
        return "region_dem_cyclades.tif"
    if use_sample():
        return "region_dem_sample.tif"
    return "region_dem.tif"


def _dem_present() -> Path | None:
    """Skip only when this request's mosaic/VRT is already built.

    A Cyclades or Solent clip must not block a full-region fetch.
    """
    mosaic = DEM_DIR / _mosaic_name()
    vrt = mosaic.with_suffix(".vrt")
    if vrt.exists() and vrt.stat().st_size > 100:
        return vrt
    if mosaic.exists() and mosaic.stat().st_size > 100_000:
        return mosaic
    stamp = DEM_DIR / ".region_complete"
    if mosaic.name == "region_dem.tif" and stamp.exists():
        return stamp
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


def _gdal_bins() -> tuple[str | None, str | None]:
    import shutil

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
    return gdalbuildvrt, gdal_translate


def _mosaic_vrt(tiles: list[Path], out_tif: Path, *, translate: bool) -> None:
    """Build a VRT of 1° COGs. Translate to GeoTIFF only for small clips."""
    import subprocess

    gdalbuildvrt, gdal_translate = _gdal_bins()
    if not gdalbuildvrt:
        print("[fetch-dem] gdalbuildvrt not found — leaving individual tiles")
        return

    vrt = out_tif.with_suffix(".vrt")
    listfile = out_tif.with_suffix(".vrt.txt")
    listfile.write_text("\n".join(str(t) for t in tiles) + "\n", encoding="utf-8")
    subprocess.run(
        [gdalbuildvrt, "-overwrite", "-input_file_list", str(listfile), str(vrt)],
        check=True,
    )
    print(f"[fetch-dem] VRT → {vrt} ({len(tiles)} tiles)")
    if not translate:
        return
    if not gdal_translate:
        print("[fetch-dem] gdal_translate not found — leaving VRT")
        return
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
    print(f"[fetch-dem] mosaicked → {out_tif} ({out_tif.stat().st_size / 1e6:.1f} MB)")


def main() -> int:
    bb = chart_bbox()
    existing = _dem_present()
    if existing and "--force" not in sys.argv:
        size = existing.stat().st_size / 1e6 if existing.stat().st_size else 0
        print(f"[fetch-dem] already present: {existing} ({size:.1f} MB)")
        return 0

    wanted = _tiles_for_bbox(bb)
    print(f"[fetch-dem] fetching {len(wanted)} GLO-30 tile(s) for {bb}")
    DEM_DIR.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, Path]] = []
    downloaded: list[Path] = []
    for lat, lon in wanted:
        name = _tile_name(lat, lon)
        dest = DEM_DIR / f"{name}.tif"
        if dest.exists() and dest.stat().st_size > 100_000:
            print(f"  ✓ cached {dest.name}")
            downloaded.append(dest)
            continue
        jobs.append((f"{BASE}/{name}/{name}.tif", dest))

    failed = 0
    if jobs:
        workers = min(8, len(jobs))
        print(f"[fetch-dem] downloading {len(jobs)} missing tile(s) ({workers} workers)")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_download, url, dest): dest for url, dest in jobs}
            for fut in as_completed(futs):
                dest = futs[fut]
                try:
                    if fut.result():
                        downloaded.append(dest)
                except Exception as exc:
                    failed += 1
                    print(f"  ! {dest.name}: {exc}", file=sys.stderr)

    if not downloaded:
        print("[fetch-dem] ERROR: no DEM tiles downloaded", file=sys.stderr)
        return 1

    mosaic = DEM_DIR / _mosaic_name()
    full_region = mosaic.name == "region_dem.tif"
    if len(downloaded) == 1 and not full_region:
        if mosaic.exists() or mosaic.is_symlink():
            mosaic.unlink()
        mosaic.symlink_to(downloaded[0].name)
        print(f"[fetch-dem] linked {mosaic.name} → {downloaded[0].name}")
    else:
        _mosaic_vrt(downloaded, mosaic, translate=not full_region)
        if full_region and failed == 0:
            (DEM_DIR / ".region_complete").write_text(
                f"{len(downloaded)} tiles\n", encoding="utf-8"
            )
        elif full_region and failed:
            print(
                f"[fetch-dem] {failed} download error(s) — not stamping complete; re-run to resume",
                file=sys.stderr,
            )

    print(f"[fetch-dem] done — {len(downloaded)} tile(s) in {DEM_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
