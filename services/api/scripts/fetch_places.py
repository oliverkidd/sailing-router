#!/usr/bin/env python3
"""One-time download of Natural Earth place / island label GeoJSON."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "geo"

# Raw GeoJSON mirrors of Natural Earth 10m cultural / physical points.
SOURCES: dict[str, str] = {
    "ne_10m_populated_places.geojson": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
        "geojson/ne_10m_populated_places.geojson"
    ),
    "ne_10m_geography_regions_points.geojson": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
        "geojson/ne_10m_geography_regions_points.geojson"
    ),
    "ne_10m_geography_regions_polys.geojson": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
        "geojson/ne_10m_geography_regions_polys.geojson"
    ),
    "ne_110m_land.geojson": (
        "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
        "geojson/ne_110m_land.geojson"
    ),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, url in SOURCES.items():
        target = OUT / name
        if target.exists() and target.stat().st_size > 1000:
            print(f"[places] already present: {target.name}")
            continue
        print(f"[places] downloading {name} …")
        try:
            resp = httpx.get(url, timeout=120.0, follow_redirects=True)
            resp.raise_for_status()
            target.write_bytes(resp.content)
            print(f"[places] wrote {target} ({len(resp.content)} bytes)")
        except Exception as exc:  # noqa: BLE001
            print(f"[places] failed {name}: {exc}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
