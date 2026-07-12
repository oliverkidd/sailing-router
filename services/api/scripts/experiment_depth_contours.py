#!/usr/bin/env python3
"""Yarmouth / West Solent depth-contour lab.

Bakes GeoJSON variants for A/B on the map. Does NOT touch depth.pmtiles.

Window is centred on Yarmouth Harbour (Isle of Wight) / western Solent so we can
judge shallow continuity where OSM waterways cut into land.

Variants
--------
  soft_clip          Current production recipe (soft land + OSM clip + DP + 2× Chaikin)
  soft_noclip        Same but no land clip — lines continue under land / into harbours
  soft_noclip_1m     No clip + 1 m isobath
  soft_noclip_05_1m  No clip + 0.5 m + 1 m isobaths
  soft_noclip_raw    Soft land, no clip, no Chaikin (continuity without smoothing)

Each variant also includes shallow fill bands (0–2, 2–4, 4–5, 5–10 m) as
Polygon features with properties.kind = "band".

Usage
-----
  cd services/api && .venv/bin/python scripts/experiment_depth_contours.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_depth_tiles import (  # noqa: E402
    REGION_TIF,
    _chaikin,
    _find_osm_shp,
    _iter_line_geoms,
    _load_land_index,
    _write_soft_land_tif,
)
from region import DEPTH_LEVELS_M  # noqa: E402

# Tight window around Yarmouth Harbour + western Solent (Hill Head / Freshwater).
YARMOUTH_BBOX = {
    "lon_min": -1.62,
    "lat_min": 50.66,
    "lon_max": -1.15,
    "lat_max": 50.84,
}

WORK = ROOT / "data" / "cache" / "tiles_build" / "depth_experiments_yarmouth"
OUT_DIR = ROOT / "apps" / "web" / "public" / "geo" / "depth-variants"
SAMPLE_TIF = WORK / "yarmouth_depth.tif"
SOFT_TIF = WORK / "yarmouth_depth_soft.tif"

_SIMPLIFY_TOL_DEG = 0.0003
_CHAIKIN_ITERS = 2
_MIN_LINE_POINTS = 4
_MIN_LINE_LENGTH_DEG = 0.001
_MIN_LINE_LENGTH_KEEP_DEG = 0.0002

# Fill bands are built from these isobath edges (same smoothed lines drawn on the map).
_BAND_EDGE_DEPTHS_M = (2.0, 4.0, 5.0, 10.0, 50.0)
_SHALLOW_BANDS = (
    (0.0, 2.0, "0-2"),
    (2.0, 4.0, "2-4"),
    (4.0, 5.0, "4-5"),
    (5.0, 10.0, "5-10"),
    (10.0, 50.0, "10-50"),
    (50.0, 1.0e6, "50+"),
)

_VARIANT_LABELS = {
    "soft_clip": "Baseline (clip)",
    "soft_noclip": "No land clip",
    "soft_noclip_1m": "No clip + 1 m",
    "soft_noclip_05_1m": "No clip + 0.5 / 1 m",
    "soft_noclip_raw": "No clip, no Chaikin",
}


def _window_tif() -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    if not REGION_TIF.exists():
        raise SystemExit(
            f"Missing {REGION_TIF} — run scripts/build_depth_tiles.py once first."
        )
    if SAMPLE_TIF.exists():
        SAMPLE_TIF.unlink()
    cmd = [
        "gdal_translate",
        "-projwin",
        str(YARMOUTH_BBOX["lon_min"]),
        str(YARMOUTH_BBOX["lat_max"]),
        str(YARMOUTH_BBOX["lon_max"]),
        str(YARMOUTH_BBOX["lat_min"]),
        "-co",
        "COMPRESS=DEFLATE",
        str(REGION_TIF),
        str(SAMPLE_TIF),
    ]
    print("[exp]", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    return SAMPLE_TIF


def _contour_gpkg(soft: Path, levels: tuple[float, ...], gpkg: Path) -> Path:
    if gpkg.exists():
        gpkg.unlink()
    cmd = [
        "gdal_contour",
        "-a",
        "depth",
        "-fl",
        *[str(v) for v in levels],
        str(soft),
        str(gpkg),
    ]
    print("[exp] gdal_contour:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    return gpkg


def _gpkg_to_features(gpkg: Path) -> list[dict]:
    raw = WORK / f"{gpkg.stem}.geojsonl"
    if raw.exists():
        raw.unlink()
    subprocess.run(
        ["ogr2ogr", "-f", "GeoJSONSeq", str(raw), str(gpkg)],
        check=True,
    )
    feats: list[dict] = []
    with raw.open() as inp:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            feats.append(json.loads(line))
    return feats


def _classify_band(depth_m: float) -> tuple[float, float, str] | None:
    if not np.isfinite(depth_m) or depth_m < 0:
        return None
    for lo, hi, name in _SHALLOW_BANDS:
        if lo <= depth_m < hi:
            return lo, hi, name
    # Deeper than last open interval (50+) uses inclusive upper via huge hi.
    return None


def _sample_depth(ds, lon: float, lat: float) -> float:
    """Nearest-neighbour depth from soft mosaic (metres, positive-down)."""
    import rasterio.windows  # noqa: PLC0415

    row, col = ds.index(lon, lat)
    if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
        return float("nan")
    val = float(ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0])
    return val


def _bands_from_isobaths(isobaths: list[dict], soft: Path) -> list[dict]:
    """Fill polygons bounded by the same smoothed 2/4/5/10 m isobaths (+ window edge).

    Raster class-polygonize is stair-stepped; this uses the vector lines already drawn
    so fills sit exactly between contours.
    """
    import rasterio  # noqa: PLC0415
    from shapely.geometry import LineString, box, mapping, shape  # noqa: PLC0415
    from shapely.ops import polygonize, unary_union  # noqa: PLC0415

    edge_depths = {float(d) for d in _BAND_EDGE_DEPTHS_M}
    parts: list[LineString] = []
    for feat in isobaths:
        props = feat.get("properties") or {}
        try:
            d = float(props.get("depth"))
        except (TypeError, ValueError):
            continue
        if d not in edge_depths:
            continue
        try:
            geom = shape(feat["geometry"])
        except Exception:  # noqa: BLE001
            continue
        for g in _iter_line_geoms(geom):
            if g.is_empty or len(g.coords) < 2:
                continue
            parts.append(g)

    bbox_poly = box(
        YARMOUTH_BBOX["lon_min"],
        YARMOUTH_BBOX["lat_min"],
        YARMOUTH_BBOX["lon_max"],
        YARMOUTH_BBOX["lat_max"],
    )
    parts.append(LineString(bbox_poly.exterior.coords))

    if len(parts) < 2:
        print("[exp]   no band-edge isobaths — skipping fills", flush=True)
        return []

    print(
        f"[exp] polygonizing fills from {len(parts) - 1} isobath pieces + bbox …",
        flush=True,
    )
    try:
        noded = unary_union(parts)
    except Exception as exc:  # noqa: BLE001
        print(f"[exp]   union failed: {exc}", flush=True)
        return []

    polys = []
    for p in polygonize(noded):
        if p.is_empty or p.area < 1e-12:
            continue
        try:
            inter = p.intersection(bbox_poly)
        except Exception:  # noqa: BLE001
            continue
        if inter.is_empty:
            continue
        if inter.geom_type == "Polygon":
            polys.append(inter)
        elif inter.geom_type == "MultiPolygon":
            polys.extend(g for g in inter.geoms if not g.is_empty and g.area > 1e-12)

    by_band: dict[str, list] = {name: [] for _, _, name in _SHALLOW_BANDS}
    with rasterio.open(soft) as ds:
        for poly in polys:
            pt = poly.representative_point()
            depth = _sample_depth(ds, pt.x, pt.y)
            classified = _classify_band(depth)
            if classified is None:
                continue
            _lo, _hi, name = classified
            by_band[name].append(poly)

    out: list[dict] = []
    for lo, hi, name in _SHALLOW_BANDS:
        geoms = by_band[name]
        if not geoms:
            continue
        try:
            merged = unary_union(geoms)
        except Exception:  # noqa: BLE001
            merged = geoms[0]
            for g in geoms[1:]:
                merged = merged.union(g)
        pieces = []
        if merged.geom_type == "Polygon":
            pieces = [merged]
        elif merged.geom_type == "MultiPolygon":
            pieces = list(merged.geoms)
        for g in pieces:
            if g.is_empty or g.area < 1e-12:
                continue
            out.append(
                {
                    "type": "Feature",
                    "properties": {
                        "kind": "band",
                        "band": name,
                        "depth_lo": lo,
                        "depth_hi": hi,
                    },
                    "geometry": mapping(g),
                }
            )
    print(f"[exp]   {len(out)} band polygons (vector-bounded)", flush=True)
    return out


def _clean_noclip(
    feat: dict,
    *,
    chaikin_iters: int,
    min_length: float,
) -> list[dict]:
    """Simplify + optional Chaikin — no OSM land difference."""
    from shapely.geometry import LineString, mapping, shape  # noqa: PLC0415

    props = feat.get("properties") or {}
    depth = props.get("depth", props.get("ELEV", props.get("elev")))
    try:
        depth_f = float(depth)
    except (TypeError, ValueError):
        return []
    if depth_f <= 0:
        return []
    depth_v: float | int
    if abs(depth_f - round(depth_f)) < 1e-6:
        depth_v = int(round(depth_f))
    else:
        depth_v = round(depth_f, 2)

    try:
        line = shape(feat["geometry"])
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for g in _iter_line_geoms(line):
        if len(g.coords) < _MIN_LINE_POINTS:
            continue
        try:
            simplified = g.simplify(_SIMPLIFY_TOL_DEG, preserve_topology=False)
        except Exception:  # noqa: BLE001
            simplified = g
        for piece in _iter_line_geoms(simplified):
            if (
                piece.is_empty
                or piece.geom_type != "LineString"
                or len(piece.coords) < _MIN_LINE_POINTS
                or piece.length < min_length
            ):
                continue
            coords = list(piece.coords)
            if chaikin_iters > 0:
                coords = _chaikin(coords, chaikin_iters)
                if len(coords) < _MIN_LINE_POINTS:
                    continue
                piece = LineString(coords)
            out.append(
                {
                    "type": "Feature",
                    "properties": {"kind": "isobath", "depth": depth_v},
                    "geometry": mapping(piece),
                }
            )
    return out


def _tag_isobaths(feats: list[dict]) -> list[dict]:
    for f in feats:
        props = f.setdefault("properties", {})
        props["kind"] = "isobath"
        if "depth" not in props:
            d = props.get("ELEV", props.get("elev"))
            if d is not None:
                props["depth"] = int(round(float(d)))
    return feats


def _write_fc(path: Path, features: list[dict], meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "features": features,
        "properties": meta,
    }
    path.write_text(json.dumps(fc, separators=(",", ":")))
    n_band = sum(1 for f in features if (f.get("properties") or {}).get("kind") == "band")
    n_line = len(features) - n_band
    print(f"[exp] wrote {path.name}: {n_line} isobaths + {n_band} bands", flush=True)


def _bake_variant(
    vid: str,
    *,
    levels: tuple[float, ...],
    land_clip: bool,
    chaikin_iters: int,
    min_length: float,
    soft: Path,
    land_tree,
    land_geoms,
) -> dict:
    gpkg = WORK / f"contours_{vid}.gpkg"
    _contour_gpkg(soft, levels, gpkg)
    raw_feats = _gpkg_to_features(gpkg)
    cleaned: list[dict] = []
    for feat in raw_feats:
        cleaned.extend(
            _clean_noclip(
                feat,
                chaikin_iters=chaikin_iters,
                min_length=min_length,
            )
        )
    # Optional land clip for baseline A/B only.
    if land_clip and land_tree is not None and land_geoms:
        from shapely.geometry import mapping, shape  # noqa: PLC0415
        from shapely.ops import unary_union  # noqa: PLC0415

        clipped: list[dict] = []
        for feat in cleaned:
            try:
                line = shape(feat["geometry"])
                idxs = land_tree.query(line)
                hits = [land_geoms[i] for i in idxs]
                if hits:
                    line = line.difference(unary_union(hits))
                for g in _iter_line_geoms(line):
                    if g.is_empty or len(g.coords) < 2:
                        continue
                    clipped.append(
                        {
                            "type": "Feature",
                            "properties": dict(feat.get("properties") or {}),
                            "geometry": mapping(g),
                        }
                    )
            except Exception:  # noqa: BLE001
                clipped.append(feat)
        cleaned = clipped

    bands = _bands_from_isobaths(cleaned, soft)
    features = [*bands, *cleaned]
    meta = {
        "id": vid,
        "levels": list(levels),
        "land_clip": land_clip,
        "chaikin_iters": chaikin_iters,
        "min_length_deg": min_length,
        "bbox": YARMOUTH_BBOX,
        "bands": [b[2] for b in _SHALLOW_BANDS],
        "band_mode": "isobath_polygonize",
    }
    _write_fc(OUT_DIR / f"{vid}.geojson", features, meta)
    return {
        "id": vid,
        "label": _VARIANT_LABELS.get(vid, vid),
        "url": f"/geo/depth-variants/{vid}.geojson",
        "features": len(cleaned),
        "band_features": len(bands),
        **meta,
    }


def main() -> int:
    print("[exp] Yarmouth depth-contour lab", flush=True)
    print(f"[exp] bbox {YARMOUTH_BBOX}", flush=True)
    _window_tif()
    if SOFT_TIF.exists():
        SOFT_TIF.unlink()
    _write_soft_land_tif(SAMPLE_TIF, SOFT_TIF)

    land_tree, land_geoms = _load_land_index()
    if _find_osm_shp() is None:
        print("[exp] warning: no OSM land — soft_clip will match soft_noclip", flush=True)

    common = dict(
        soft=SOFT_TIF,
        land_tree=land_tree,
        land_geoms=land_geoms,
    )
    variants = [
        _bake_variant(
            "soft_clip",
            levels=DEPTH_LEVELS_M,
            land_clip=True,
            chaikin_iters=_CHAIKIN_ITERS,
            min_length=_MIN_LINE_LENGTH_DEG,
            **common,
        ),
        _bake_variant(
            "soft_noclip",
            levels=DEPTH_LEVELS_M,
            land_clip=False,
            chaikin_iters=_CHAIKIN_ITERS,
            min_length=_MIN_LINE_LENGTH_DEG,
            **common,
        ),
        _bake_variant(
            "soft_noclip_1m",
            levels=(1.0, *DEPTH_LEVELS_M),
            land_clip=False,
            chaikin_iters=_CHAIKIN_ITERS,
            min_length=_MIN_LINE_LENGTH_KEEP_DEG,
            **common,
        ),
        _bake_variant(
            "soft_noclip_05_1m",
            levels=(0.5, 1.0, *DEPTH_LEVELS_M),
            land_clip=False,
            chaikin_iters=_CHAIKIN_ITERS,
            min_length=_MIN_LINE_LENGTH_KEEP_DEG,
            **common,
        ),
        _bake_variant(
            "soft_noclip_raw",
            levels=DEPTH_LEVELS_M,
            land_clip=False,
            chaikin_iters=0,
            min_length=_MIN_LINE_LENGTH_KEEP_DEG,
            **common,
        ),
    ]

    manifest = {
        "bbox": YARMOUTH_BBOX,
        "center": [
            (YARMOUTH_BBOX["lat_min"] + YARMOUTH_BBOX["lat_max"]) / 2,
            (YARMOUTH_BBOX["lon_min"] + YARMOUTH_BBOX["lon_max"]) / 2,
        ],
        "zoom": 12,
        "note": "Production depth.pmtiles unchanged. Toggle variants in DepthContourLab.",
        "variants": variants,
    }
    (OUT_DIR / "index.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[exp] manifest → {OUT_DIR / 'index.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
