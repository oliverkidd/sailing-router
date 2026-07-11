#!/usr/bin/env python3
"""One-time download of GSHHG high-resolution coastline shapefiles."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "geo" / "gshhg"
URL = "http://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip"
NEEDED = ("GSHHS_h_L1.shp", "GSHHS_h_L1.dbf", "GSHHS_h_L1.shx", "GSHHS_h_L1.prj")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if all((OUT / name).exists() for name in NEEDED):
        print(f"[gshhg] already present in {OUT}")
        return 0

    print(f"[gshhg] downloading {URL} …")
    resp = httpx.get(URL, timeout=300.0, follow_redirects=True)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if name.startswith("GSHHS_h_L1."):
                target = OUT / name
                target.write_bytes(zf.read(info.filename))
                print(f"[gshhg] wrote {target.name}")

    missing = [n for n in NEEDED if not (OUT / n).exists()]
    if missing:
        print(f"[gshhg] missing after extract: {missing}", file=sys.stderr)
        return 1
    print("[gshhg] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
