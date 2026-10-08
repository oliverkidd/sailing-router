#!/usr/bin/env python3
"""Resumable full-chart bake: DEM → Terrain-RGB/shade → WindNinja 400 m.

Uses REGION_BBOX (the chart) as the source of truth. Each step is idempotent
and can be re-run; already-published Cyclades tiles / the Aegean WindNinja
library are skipped.

Usage:
  python scripts/build_full_chart.py
  python scripts/build_full_chart.py --skip-dem
  python scripts/build_full_chart.py --only dem|terrain-rgb|wind-terrain
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_API = _SCRIPTS.parent
_PY = sys.executable


def _run(script: str) -> int:
    cmd = [_PY, str(_SCRIPTS / script)]
    print(f"\n══ {script} ══\n", flush=True)
    return subprocess.call(cmd, cwd=str(_API))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-dem", action="store_true")
    ap.add_argument(
        "--only",
        choices=("dem", "terrain-rgb", "wind-terrain"),
        default=None,
    )
    args = ap.parse_args()

    steps: list[tuple[str, str]] = []
    if args.only == "dem" or (args.only is None and not args.skip_dem):
        steps.append(("dem", "fetch_dem.py"))
    if args.only in (None, "terrain-rgb"):
        steps.append(("terrain-rgb", "build_terrain_rgb.py"))
    if args.only in (None, "wind-terrain"):
        steps.append(("wind-terrain", "build_wind_terrain.py"))

    for name, script in steps:
        code = _run(script)
        if code != 0:
            print(f"[full-chart] {name} failed with {code}", file=sys.stderr)
            return code
    print("\n[full-chart] done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
