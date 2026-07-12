#!/usr/bin/env python3
"""Fetch coastal OSM chart features via tiled Overpass (full REGION_BBOX).

Skips inland tiles using the ~10 km sea-coast band from coastal_band.py.
Writes GeoJSONSeq for tippecanoe:

  data/cache/tiles_build/osm_places.geojsonl
  data/cache/tiles_build/osm_roads.geojsonl
  data/cache/tiles_build/osm_rivers.geojsonl
  data/cache/tiles_build/osm_water.geojsonl

Prefer this over extract_osm_pbf_features.py (multi-GB country dumps).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import orient, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coastal_band import OUT_PATH as BAND_PATH  # noqa: E402
from coastal_band import build_coastal_band, load_band_geom  # noqa: E402
from region import chart_bbox, use_sample  # noqa: E402

WORK = ROOT / "data" / "cache" / "tiles_build"
CACHE = ROOT / "data" / "cache" / "overpass"

_TILE_DEG = 2.0
_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/cgi/interpreter",
)

# Chart-scale labels — no villages/hamlets (too dense inland).
_PLACE_META: dict[str, tuple[int, int, str]] = {
    "city": (0, 4, "city"),
    "town": (2, 6, "town"),
    "island": (1, 5, "island"),
    "archipelago": (1, 5, "island"),
}

_ROAD_META: dict[str, tuple[int, str]] = {
    "motorway": (5, "motorway"),
    "motorway_link": (6, "motorway"),
    "trunk": (6, "trunk"),
    "trunk_link": (7, "trunk"),
    "primary": (7, "primary"),
    "primary_link": (8, "primary"),
}


def _tiles() -> list[tuple[float, float, float, float]]:
    bb = chart_bbox()
    lon0, lat0 = bb["lon_min"], bb["lat_min"]
    lon1, lat1 = bb["lon_max"], bb["lat_max"]
    out: list[tuple[float, float, float, float]] = []
    lat = lat0
    while lat < lat1 - 1e-9:
        lon = lon0
        north = min(lat + _TILE_DEG, lat1)
        while lon < lon1 - 1e-9:
            east = min(lon + _TILE_DEG, lon1)
            out.append((lat, lon, north, east))  # south, west, north, east
            lon = east
        lat = north

    priority = [
        (50.7, -1.3),  # Isle of Wight / Solent
        (36.4, 25.4),  # Santorini
        (37.9, 23.7),  # Athens / Saronic
        (43.3, 5.4),  # Marseille
        (41.9, 12.5),  # Rome / Tyrrhenian
        (39.0, 1.4),  # Balearics
        (35.5, -5.5),  # Gibraltar / Strait
        (53.3, -6.2),  # Dublin
        (55.9, -4.2),  # Clyde
        (60.4, 5.3),  # Bergen
    ]

    def score(tile: tuple[float, float, float, float]) -> float:
        south, west, north, east = tile
        cy, cx = (south + north) / 2, (west + east) / 2
        return min((cy - py) ** 2 + (cx - px) ** 2 for py, px in priority)

    out.sort(key=score)
    return out


def _query_detail(south: float, west: float, north: float, east: float) -> str:
    """Places, roads, named river centerlines (labels)."""
    bb = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:180];
(
  node["place"~"^(city|town|island|archipelago)$"]["name"]({bb});
  way["place"~"^(island|archipelago)$"]["name"]({bb});
  relation["place"~"^(island|archipelago)$"]["name"]({bb});
  node["harbour"~"^(yes|harbour|port)$"]["name"]({bb});
  node["amenity"="marina"]["name"]({bb});
  node["industrial"="harbour"]["name"]({bb});
  node["seamark:type"~"^(harbour|harbour_basin|marina)$"]["name"]({bb});
  node["place"="harbour"]["name"]({bb});
  way["harbour"~"^(yes|harbour|port)$"]["name"]({bb});
  way["amenity"="marina"]["name"]({bb});
  way["seamark:type"~"^(harbour|harbour_basin|marina)$"]["name"]({bb});
  relation["harbour"~"^(yes|harbour|port)$"]["name"]({bb});
  relation["amenity"="marina"]["name"]({bb});
);
out center tags;
(
  way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link)$"]({bb});
  way["waterway"~"^(river|canal)$"]["name"]({bb});
);
out geom tags;
""".strip()


def _query_water(south: float, west: float, north: float, east: float) -> str:
    """Water-area multipolygons — dedicated `out geom` so relation members include rings.

    Mixed place/road queries with `out geom tags` drop relation member geometries
    (e.g. River Medina rel 4091032), which is why estuaries rendered as thin lines.
    """
    bb = f"{south},{west},{north},{east}"
    return f"""
[out:json][timeout:180];
(
  way["natural"="water"]({bb});
  relation["natural"="water"]({bb});
  way["waterway"="riverbank"]({bb});
  relation["waterway"="riverbank"]({bb});
);
out geom;
""".strip()


def _overpass_post(query: str, cache_path: Path) -> dict:
    if cache_path.exists() and cache_path.stat().st_size > 50:
        return json.loads(cache_path.read_text())

    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(8):
        endpoint = _ENDPOINTS[attempt % len(_ENDPOINTS)]
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "MasterWindChartBuilder/1.0 (sailing weather router)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()
            data = json.loads(raw)
            if "elements" not in data:
                raise json.JSONDecodeError(
                    "missing elements", raw.decode("utf-8", "replace")[:200], 0
                )
            cache_path.write_bytes(raw)
            return data
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            last_err = exc
            wait = min(60, 6 * (attempt + 1))
            print(
                f"[overpass] retry {attempt + 1} via {endpoint.split('/')[2]} "
                f"after {exc} (sleep {wait}s)",
                flush=True,
            )
            time.sleep(wait)
    raise RuntimeError(f"Overpass failed for {cache_path.name}: {last_err}")


def _fetch_query_cached(
    kind: str,
    south: float,
    west: float,
    north: float,
    east: float,
    query_fn,
) -> dict:
    """Fetch one Overpass query; on timeout, split the bbox into quadrants."""
    key = f"v4_{kind}_{south:.2f}_{west:.2f}_{north:.2f}_{east:.2f}"
    cache_path = CACHE / f"{key}.json"
    try:
        return _overpass_post(query_fn(south, west, north, east), cache_path)
    except RuntimeError:
        # Split when the cell is still large enough to matter.
        if (north - south) < 0.6 or (east - west) < 0.6:
            raise
        mid_lat = (south + north) / 2
        mid_lon = (west + east) / 2
        print(
            f"[overpass] split {kind} "
            f"({west:.1f},{south:.1f})–({east:.1f},{north:.1f}) → quads",
            flush=True,
        )
        parts = [
            _fetch_query_cached(kind, south, west, mid_lat, mid_lon, query_fn),
            _fetch_query_cached(kind, south, mid_lon, mid_lat, east, query_fn),
            _fetch_query_cached(kind, mid_lat, west, north, mid_lon, query_fn),
            _fetch_query_cached(kind, mid_lat, mid_lon, north, east, query_fn),
        ]
        merged: list = []
        for p in parts:
            merged.extend(p.get("elements") or [])
        data = {"elements": merged}
        cache_path.write_text(json.dumps(data))
        return data


def _fetch_tile(south: float, west: float, north: float, east: float) -> dict:
    """Merge detail + water queries. Cache key v4 = 2° tiles + split-on-timeout."""
    detail = _fetch_query_cached("detail", south, west, north, east, _query_detail)
    water = _fetch_query_cached("water", south, west, north, east, _query_water)
    return {
        "elements": list(detail.get("elements") or []) + list(water.get("elements") or [])
    }

def _way_coords(el: dict) -> list[list[float]] | None:
    geom = el.get("geometry")
    if not geom or len(geom) < 2:
        return None
    return [[float(p["lon"]), float(p["lat"])] for p in geom]


def _point_coords(el: dict) -> list[float] | None:
    if "lat" in el and "lon" in el:
        return [float(el["lon"]), float(el["lat"])]
    c = el.get("center")
    if c and "lat" in c and "lon" in c:
        return [float(c["lon"]), float(c["lat"])]
    return None


def _ring_closed(coords: list[list[float]]) -> list[list[float]]:
    if coords[0] != coords[-1]:
        return coords + [coords[0]]
    return coords


def _polygon_from_way(el: dict) -> Polygon | None:
    coords = _way_coords(el)
    if not coords or len(coords) < 3:
        return None
    ring = _ring_closed(coords)
    try:
        poly = make_valid(Polygon(ring))
    except Exception:  # noqa: BLE001
        return None
    if poly.is_empty or poly.area <= 0:
        return None
    return poly if poly.geom_type in {"Polygon", "MultiPolygon"} else None


def _polygons_from_relation(el: dict) -> list:
    """Assemble OSM multipolygon relations from Overpass `out geom` members.

    Outer members are usually *open* ways that must be chained into rings.
    Closing each member alone (old behaviour) shatters rivers into dozens of
    bogus polygons with straight tile-like cuts — the Medina / Yar artifacts.
    """
    from shapely.geometry import LineString
    from shapely.ops import polygonize

    members = el.get("members") or []
    outer_lines: list = []
    inner_lines: list = []
    for m in members:
        geom = m.get("geometry")
        if not geom or len(geom) < 2:
            continue
        coords = [(float(p["lon"]), float(p["lat"])) for p in geom]
        try:
            line = LineString(coords)
        except Exception:  # noqa: BLE001
            continue
        if line.is_empty or line.length == 0:
            continue
        if m.get("role") == "inner":
            inner_lines.append(line)
        else:
            outer_lines.append(line)
    if not outer_lines:
        return []

    try:
        outers = [make_valid(p) for p in polygonize(unary_union(outer_lines))]
        outers = [p for p in outers if not p.is_empty and p.area > 0]
    except Exception:  # noqa: BLE001
        outers = []
    if not outers:
        # Fallback: already-closed single-ring members.
        for m in members:
            if m.get("role") == "inner":
                continue
            geom = m.get("geometry") or []
            if len(geom) < 3:
                continue
            coords = [[float(p["lon"]), float(p["lat"])] for p in geom]
            if coords[0] != coords[-1]:
                continue
            try:
                poly = make_valid(Polygon(coords))
            except Exception:  # noqa: BLE001
                continue
            if not poly.is_empty and poly.area > 0:
                outers.append(poly)
    if not outers:
        return []

    try:
        base = unary_union(outers) if len(outers) > 1 else outers[0]
        if inner_lines:
            inners = [make_valid(p) for p in polygonize(unary_union(inner_lines))]
            inners = [p for p in inners if not p.is_empty and p.area > 0]
            if inners:
                holes = unary_union(inners) if len(inners) > 1 else inners[0]
                base = make_valid(base.difference(holes))
        else:
            base = make_valid(base)
    except Exception:  # noqa: BLE001
        return outers

    if base.is_empty:
        return []
    if base.geom_type == "Polygon":
        return [base]
    if base.geom_type == "MultiPolygon":
        return [p for p in base.geoms if not p.is_empty and p.area > 0]
    return []


# Keep estuaries / tidal rivers / harbour basins; drop garden ponds etc.
_KEEP_WATER = {
    "river",
    "tidal",
    "estuary",
    "harbour",
    "basin",
    "lagoon",
    "canal",
    "stream",
    "oxbow",
}
_REJECT_WATER = {
    "pond",
    "fishpond",
    "wastewater",
    "swimming_pool",
    "reflecting_pool",
    "fountain",
    "ditch",
    "moat",
}
# ~2e-7 deg² ≈ 2–3 × 10³ m² at mid-latitudes — drops garden ponds.
_MIN_WATER_AREA = 2.0e-7
# Untagged / lake / reservoir only if clearly substantial.
_LARGE_WATER_AREA = 2.0e-6


def _water_feature_ok(tags: dict, area: float) -> bool:
    if area < _MIN_WATER_AREA:
        return False
    if tags.get("waterway") == "riverbank":
        return True
    water = (tags.get("water") or "").strip().lower()
    if water in _REJECT_WATER:
        return False
    if water in _KEEP_WATER or tags.get("tidal") == "yes":
        return True
    # Generic natural=water / lake / reservoir — only larger bodies.
    return area >= _LARGE_WATER_AREA


def _is_harbour(tags: dict) -> bool:
    if tags.get("harbour") in {"yes", "harbour", "port"}:
        return True
    if tags.get("amenity") == "marina":
        return True
    if tags.get("industrial") == "harbour":
        return True
    if tags.get("seamark:type") in {"harbour", "harbour_basin", "marina"}:
        return True
    if tags.get("place") == "harbour":
        return True
    return False


_HARBOUR_NAME_BAD = (
    "sailing club",
    "yacht club",
    "boat yard",
    "boatyard",
    "boathouse",
    "boat house",
    "sail training",
    "university",
    "joint services",
    "rowing",
    "dinghy",
    "proposed",
)
_HARBOUR_NAME_KEEP = (
    "harbour",
    "harbor",
    "marina",
    "haven",
    "port",
    "quay",
    "dock",
)


def _harbour_name_ok(name: str) -> bool:
    n = name.lower().strip()
    if not n or n in {"harbour office", "harbor office", "marina office", "boat yard", "boatyard"}:
        return False
    if any(b in n for b in _HARBOUR_NAME_BAD):
        return False
    # Chart-scale: named harbours / marinas / havens — not every boatyard amenity.
    return any(k in n for k in _HARBOUR_NAME_KEEP)

def _clip_point(coords: list[float], band_prep) -> bool:
    return band_prep.contains(Point(coords[0], coords[1]))


def _clip_line(coords: list[list[float]], band_prep, band_geom) -> list[list[float]] | None:
    try:
        line = LineString(coords)
    except Exception:  # noqa: BLE001
        return None
    if line.is_empty or line.length == 0:
        return None
    if not band_prep.intersects(line):
        return None
    clipped = line.intersection(band_geom)
    if clipped.is_empty:
        return None
    if clipped.geom_type == "LineString":
        return [list(c) for c in clipped.coords]
    if clipped.geom_type == "MultiLineString":
        # Keep longest segment for tippecanoe simplicity.
        parts = list(clipped.geoms)
        best = max(parts, key=lambda g: g.length)
        return [list(c) for c in best.coords]
    return None


def _clip_poly(poly, band_prep, band_geom):
    """Keep water that intersects the coast band — do not geometric-clip.

    Intersecting complex rivers with the band polygon shatters them along
    band/tile edges (straight vertical cuts through Medina / Yar).
    """
    if poly is None or poly.is_empty:
        return None
    if not band_prep.intersects(poly):
        return None
    return poly


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    if use_sample():
        print("[overpass] SAMPLE mode — Isle of Wight / Solent only")

    # Always rebuild band in sample mode so we do not reuse a Europe-scale file.
    # Sample band is tiny (~KB); full Europe band is multi-MB. Never reuse
    # a Solent preview band for a full-region crawl.
    band_ok = BAND_PATH.exists() and BAND_PATH.stat().st_size >= (
        1000 if use_sample() else 2_000_000
    )
    need_band = not band_ok or use_sample()
    if need_band:
        print("[overpass] building coastal band …")
        build_coastal_band(force=True)
    band_geom = load_band_geom()
    # Explode for STRtree query performance on large multipolygons.
    if band_geom.geom_type == "MultiPolygon":
        band_geoms = list(band_geom.geoms)
    elif band_geom.geom_type == "Polygon":
        band_geoms = [band_geom]
    else:
        band_geoms = [g for g in getattr(band_geom, "geoms", [band_geom]) if not g.is_empty]
    band_tree = STRtree(band_geoms)
    band_prep = prep(band_geom)
    print(f"[overpass] coastal band ready ({len(band_geoms)} parts)")

    places_path = WORK / "osm_places.geojsonl"
    roads_path = WORK / "osm_roads.geojsonl"
    rivers_path = WORK / "osm_rivers.geojsonl"
    water_path = WORK / "osm_water.geojsonl"

    place_seen: set[str] = set()
    road_seen: set[int] = set()
    river_seen: set[int] = set()
    water_seen: set[int] = set()

    n_places = n_roads = n_rivers = n_water = 0
    n_skip = 0
    tiles = _tiles()
    bb = chart_bbox()
    print(
        f"[overpass] {len(tiles)} tiles at {_TILE_DEG}° "
        f"covering ({bb['lon_min']},{bb['lat_min']})–({bb['lon_max']},{bb['lat_max']})"
    )

    with (
        places_path.open("w") as fp_places,
        roads_path.open("w") as fp_roads,
        rivers_path.open("w") as fp_rivers,
        water_path.open("w") as fp_water,
    ):
        for i, (south, west, north, east) in enumerate(tiles, start=1):
            from shapely.geometry import box as shapely_box

            tile = shapely_box(west, south, east, north)
            hits = band_tree.query(tile)
            if len(hits) == 0 or not band_prep.intersects(tile):
                n_skip += 1
                if i % 25 == 0:
                    print(f"[overpass] tile {i}/{len(tiles)} (skipped inland so far={n_skip})")
                continue

            print(
                f"[overpass] tile {i}/{len(tiles)} "
                f"({west:.1f},{south:.1f})–({east:.1f},{north:.1f})"
            )
            try:
                data = _fetch_tile(south, west, north, east)
            except Exception as exc:  # noqa: BLE001
                print(f"[overpass] SKIP tile after failure: {exc}", file=sys.stderr)
                continue

            for el in data.get("elements") or []:
                tags = el.get("tags") or {}
                eid = int(el.get("id") or 0)
                etype = el.get("type")

                # --- water areas (ways / relations with geometry) ---
                is_water = tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
                if is_water and etype in {"way", "relation"}:
                    if eid in water_seen:
                        continue
                    polys = []
                    if etype == "way":
                        p = _polygon_from_way(el)
                        if p is not None:
                            polys.append(p)
                    else:
                        polys.extend(_polygons_from_relation(el))
                    if not polys:
                        continue
                    try:
                        poly = unary_union(polys) if len(polys) > 1 else polys[0]
                    except Exception:  # noqa: BLE001
                        continue
                    if not _water_feature_ok(tags, float(poly.area)):
                        continue
                    clipped = _clip_poly(poly, band_prep, band_geom)
                    if clipped is None:
                        continue
                    if not _water_feature_ok(tags, float(clipped.area)):
                        continue
                    water_seen.add(eid)
                    name = (tags.get("name") or "").strip()
                    # GeoJSON exterior rings must be CCW or tippecanoe can drop fills.
                    oriented = orient(clipped, sign=1.0)
                    feat = {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": 6, "maxzoom": 14},
                        "properties": {
                            "name": name,
                            "kind": "water",
                            "water": tags.get("water") or tags.get("waterway") or "",
                        },
                        "geometry": mapping(oriented),
                    }
                    fp_water.write(json.dumps(feat, separators=(",", ":")) + "\n")
                    n_water += 1
                    continue

                place = tags.get("place")
                if place in _PLACE_META and tags.get("name"):
                    coords = _point_coords(el)
                    if not coords:
                        continue
                    if not _clip_point(coords, band_prep):
                        continue
                    dedupe = f"{place}:{tags['name']}:{coords[0]:.4f}:{coords[1]:.4f}"
                    if dedupe in place_seen:
                        continue
                    place_seen.add(dedupe)
                    rank, minzoom, kind = _PLACE_META[place]
                    feat = {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                        "properties": {
                            "name": str(tags["name"]).strip(),
                            "kind": kind,
                            "place": place,
                            "rank": rank,
                        },
                        "geometry": {"type": "Point", "coordinates": coords},
                    }
                    fp_places.write(json.dumps(feat, separators=(",", ":")) + "\n")
                    n_places += 1
                    continue

                if _is_harbour(tags) and tags.get("name"):
                    name = str(tags["name"]).strip()
                    if not _harbour_name_ok(name):
                        continue
                    coords = _point_coords(el)
                    if not coords or not _clip_point(coords, band_prep):
                        continue
                    dedupe = f"harbour:{name}:{coords[0]:.4f}:{coords[1]:.4f}"
                    if dedupe in place_seen:
                        continue
                    place_seen.add(dedupe)
                    feat = {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": 8, "maxzoom": 14},
                        "properties": {
                            "name": name,
                            "kind": "harbour",
                            "place": "harbour",
                            "rank": 3,
                        },
                        "geometry": {"type": "Point", "coordinates": coords},
                    }
                    fp_places.write(json.dumps(feat, separators=(",", ":")) + "\n")
                    n_places += 1
                    continue

                highway = tags.get("highway")
                if highway in _ROAD_META:
                    if eid in road_seen:
                        continue
                    coords = _way_coords(el)
                    if not coords:
                        continue
                    clipped = _clip_line(coords, band_prep, band_geom)
                    if not clipped or len(clipped) < 2:
                        continue
                    road_seen.add(eid)
                    minzoom, kind = _ROAD_META[highway]
                    feat = {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                        "properties": {"kind": kind, "highway": highway},
                        "geometry": {"type": "LineString", "coordinates": clipped},
                    }
                    fp_roads.write(json.dumps(feat, separators=(",", ":")) + "\n")
                    n_roads += 1
                    continue

                waterway = tags.get("waterway")
                if waterway in {"river", "canal"} and tags.get("name"):
                    if eid in river_seen:
                        continue
                    coords = _way_coords(el)
                    if not coords:
                        continue
                    clipped = _clip_line(coords, band_prep, band_geom)
                    if not clipped or len(clipped) < 2:
                        continue
                    river_seen.add(eid)
                    name = str(tags["name"]).strip()
                    feat = {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": 7, "maxzoom": 14},
                        "properties": {
                            "name": name,
                            "kind": "river" if waterway == "river" else "canal",
                        },
                        "geometry": {"type": "LineString", "coordinates": clipped},
                    }
                    fp_rivers.write(json.dumps(feat, separators=(",", ":")) + "\n")
                    n_rivers += 1

            time.sleep(1.2)

    print(
        f"[overpass] places={n_places} roads={n_roads} rivers={n_rivers} "
        f"water={n_water} skipped_inland_tiles={n_skip}"
    )
    print(f"[overpass] → {places_path}")
    print(f"[overpass] → {roads_path}")
    print(f"[overpass] → {rivers_path}")
    print(f"[overpass] → {water_path}")
    min_places = 5 if use_sample() else 50
    if n_places < min_places:
        print(f"[overpass] too few places ({n_places} < {min_places}) — abort", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
