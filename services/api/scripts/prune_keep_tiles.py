#!/usr/bin/env python3
"""Delete chart tiles that miss the signed-off keep zone.

The zone is the union of the four boxes on the /coverage page
(apps/web/src/lib/coverageBoxes.ts). Overlap is intentional: a tile is
kept when it intersects any box. PMTiles archives are left as-is.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TERRAIN = ROOT / "apps/web/public/geo/terrain"
DEM = ROOT / "data/dem"
WIND = ROOT / "data/cache/wind_terrain"

# lon_min, lat_min, lon_max, lat_max — same numbers as coverageBoxes.ts
BOXES: list[tuple[float, float, float, float]] = [
    (-10.8, 48.2, 1.6, 52.2),  # Channel & Fastnet
    (-9.8, 35.9, 8.4, 51.2),  # France, Spain & Portugal
    (-5.8, 33.9, 20.0, 45.9),  # Med west, south edge = Gabes
    (19.5, 33.9, 36.4, 41.2),  # Med east, south edge = Gabes
]

_DEM_NAME = re.compile(
    r"_(?P<ns>[NS])(?P<lat>\d+)_00_(?P<ew>[EW])(?P<lon>\d+)_00_DEM\.tif$"
)
_WIND_NAME = re.compile(r"^tile_(?P<ew>[EW])(?P<lon>\d+)_(?P<ns>[NS])(?P<lat>\d+)$")


def _hits(west: float, south: float, east: float, north: float) -> bool:
    for lon_min, lat_min, lon_max, lat_max in BOXES:
        if east < lon_min or west > lon_max or north < lat_min or south > lat_max:
            continue
        return True
    return False


def _lon_hits(west: float, east: float) -> bool:
    return any(not (east < lon_min or west > lon_max) for lon_min, _, lon_max, _ in BOXES)


def _lat_north(z: int, y: float) -> float:
    n = 1 << z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _prune_column(item: tuple[int, str]) -> tuple[int, int, int, int]:
    """Delete one mercator x-column. Returns del_b, keep_b, del_n, keep_n."""
    z, xpath_s = item
    xpath = Path(xpath_s)
    n = 1 << z
    try:
        x = int(xpath.name)
    except ValueError:
        return 0, 0, 0, 0
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    if not _lon_hits(west, east):
        b = _tree_bytes(xpath)
        files = sum(len(fs) for _d, _s, fs in os.walk(xpath))
        shutil.rmtree(xpath, ignore_errors=True)
        return b, 0, files, 0
    del_b = keep_b = del_n = keep_n = 0
    try:
        names = list(xpath.iterdir())
    except FileNotFoundError:
        return 0, 0, 0, 0
    for ypath in names:
        if not ypath.is_file() or not ypath.stem.isdigit():
            continue
        y = int(ypath.stem)
        north = _lat_north(z, y)
        south = _lat_north(z, y + 1)
        try:
            size = ypath.stat().st_size
        except OSError:
            continue
        if _hits(west, south, east, north):
            keep_b += size
            keep_n += 1
        else:
            try:
                ypath.unlink()
            except OSError:
                continue
            del_b += size
            del_n += 1
    try:
        xpath.rmdir()
    except OSError:
        pass
    return del_b, keep_b, del_n, keep_n


def prune_xyz(root: Path) -> tuple[int, int, int, int]:
    """Return deleted_bytes, kept_bytes, deleted_files, kept_files."""
    deleted_b = kept_b = deleted_n = kept_n = 0
    if not root.is_dir():
        print(f"[prune] missing {root}", flush=True)
        return 0, 0, 0, 0
    zdirs = sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name))
    workers = min(8, os.cpu_count() or 4)
    for zpath in zdirs:
        z = int(zpath.name)
        columns = [(z, str(p)) for p in zpath.iterdir() if p.name.isdigit()]
        z_del_b = z_keep_b = z_del_n = z_keep_n = 0
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_prune_column, col) for col in columns]
            for fut in as_completed(futures):
                del_b, keep_b, del_n, keep_n = fut.result()
                z_del_b += del_b
                z_keep_b += keep_b
                z_del_n += del_n
                z_keep_n += keep_n
                done += 1
                if done % 500 == 0:
                    print(
                        f"[prune] {root.name} z{z}: {done}/{len(columns)} columns"
                        f"  deleted {z_del_b / 1e9:.2f} GB so far",
                        flush=True,
                    )
        try:
            zpath.rmdir()
        except OSError:
            pass
        deleted_b += z_del_b
        kept_b += z_keep_b
        deleted_n += z_del_n
        kept_n += z_keep_n
        print(
            f"[prune] {root.name} z{z}: kept {z_keep_n} ({z_keep_b / 1e9:.2f} GB)"
            f"  deleted {z_del_n} ({z_del_b / 1e9:.2f} GB)",
            flush=True,
        )
    return deleted_b, kept_b, deleted_n, kept_n


def _signed(kind: str, value: int) -> int:
    return value if kind in ("N", "E") else -value


def prune_dem() -> tuple[int, int, int, int]:
    deleted_b = kept_b = deleted_n = kept_n = 0
    kept: list[Path] = []
    for path in sorted(DEM.glob("*.tif")):
        m = _DEM_NAME.search(path.name)
        if not m:
            continue
        lat = _signed(m.group("ns"), int(m.group("lat")))
        lon = _signed(m.group("ew"), int(m.group("lon")))
        size = path.stat().st_size
        if _hits(lon, lat, lon + 1, lat + 1):
            kept.append(path)
            kept_b += size
            kept_n += 1
        else:
            path.unlink()
            deleted_b += size
            deleted_n += 1
    print(
        f"[prune] dem: kept {kept_n} ({kept_b / 1e9:.2f} GB)"
        f"  deleted {deleted_n} ({deleted_b / 1e9:.2f} GB)",
        flush=True,
    )
    _rebuild_vrt(kept)
    return deleted_b, kept_b, deleted_n, kept_n


def _rebuild_vrt(tiles: list[Path]) -> None:
    gdalbuildvrt = shutil.which("gdalbuildvrt")
    if not gdalbuildvrt:
        for base in ("/opt/homebrew/bin", "/Applications/Postgres.app/Contents/Versions/latest/bin"):
            candidate = Path(base) / "gdalbuildvrt"
            if candidate.exists():
                gdalbuildvrt = str(candidate)
                break
    vrt = DEM / "region_dem.vrt"
    listfile = DEM / "region_dem.vrt.txt"
    if not tiles:
        print("[prune] dem: no tiles left, leaving VRT untouched", flush=True)
        return
    listfile.write_text("\n".join(str(t) for t in tiles) + "\n", encoding="utf-8")
    if not gdalbuildvrt:
        print("[prune] dem: gdalbuildvrt not found — VRT not rebuilt", flush=True)
        return
    subprocess.run(
        [gdalbuildvrt, "-overwrite", "-input_file_list", str(listfile), str(vrt)],
        check=True,
    )
    print(f"[prune] dem VRT rebuilt ({len(tiles)} tiles)", flush=True)


def prune_wind() -> tuple[int, int, int, int]:
    deleted_b = kept_b = deleted_n = kept_n = 0
    if not WIND.is_dir():
        return 0, 0, 0, 0
    for path in list(WIND.iterdir()):
        m = _WIND_NAME.match(path.name)
        if not m or not path.is_dir():
            continue
        lon = _signed(m.group("ew"), int(m.group("lon")))
        lat = _signed(m.group("ns"), int(m.group("lat")))
        size = _tree_bytes(path)
        files = sum(len(fs) for _d, _s, fs in os.walk(path))
        if _hits(lon, lat, lon + 2, lat + 2):
            kept_b += size
            kept_n += 1
        else:
            shutil.rmtree(path)
            deleted_b += size
            deleted_n += files
    manifest = WIND / "tiles.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"tiles": []}
        tiles = []
        for entry in data.get("tiles", []):
            rel = str(entry.get("path", ""))
            if (WIND / rel).exists():
                tiles.append(entry)
        manifest.write_text(json.dumps({"tiles": tiles}, indent=2) + "\n", encoding="utf-8")
    print(
        f"[prune] wind tiles: kept {kept_n} ({kept_b / 1e9:.2f} GB)"
        f"  deleted {deleted_n} files ({deleted_b / 1e9:.2f} GB)",
        flush=True,
    )
    return deleted_b, kept_b, deleted_n, kept_n


def _write_tilejson(path: Path) -> None:
    if not path.exists():
        return
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["bounds"] = [-10.8, 33.9, 36.4, 52.2]
    meta["version"] = "3.2.0"
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"[prune] bounds → {path}", flush=True)


def main() -> None:
    rgb = prune_xyz(TERRAIN)
    shade = prune_xyz(TERRAIN / "shade")
    dem = prune_dem()
    wind = prune_wind()
    _write_tilejson(TERRAIN / "tilejson.json")
    _write_tilejson(TERRAIN / "shade" / "tilejson.json")
    labels = ("terrain-rgb", "hillshade", "dem", "wind")
    print("[prune] ---", flush=True)
    grand_del = grand_keep = 0
    for label, (deleted_b, kept_b, deleted_n, kept_n) in zip(labels, (rgb, shade, dem, wind)):
        grand_del += deleted_b
        grand_keep += kept_b
        before = deleted_b + kept_b
        print(
            f"[prune] {label}: {before / 1e9:.2f} GB → {kept_b / 1e9:.2f} GB"
            f"  (removed {deleted_b / 1e9:.2f} GB, {deleted_n} files, kept {kept_n})",
            flush=True,
        )
    print(
        f"[prune] tiled data { (grand_del + grand_keep) / 1e9:.2f} GB → {grand_keep / 1e9:.2f} GB"
        f"  (removed {grand_del / 1e9:.2f} GB)",
        flush=True,
    )
    print("PRUNE DONE", flush=True)


if __name__ == "__main__":
    main()
