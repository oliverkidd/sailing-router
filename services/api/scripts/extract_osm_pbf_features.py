#!/usr/bin/env python3
"""Extract coastal chart features from Geofabrik PBFs (full-region path).

Overpass is unreliable for continent-scale crawls (406/504). This reads local
OSM PBFs, keeps only features intersecting the ~10 km coastal band, and writes:

  data/cache/tiles_build/osm_places.geojsonl
  data/cache/tiles_build/osm_roads.geojsonl
  data/cache/tiles_build/osm_rivers.geojsonl
  data/cache/tiles_build/osm_water.geojsonl

Requires: pip osmium, shapely; run coastal_band.py first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import osmium
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import orient, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coastal_band import OUT_PATH as BAND_PATH  # noqa: E402
from coastal_band import build_coastal_band, load_band_geom  # noqa: E402
from region import chart_bbox  # noqa: E402

WORK = ROOT / "data" / "cache" / "tiles_build"
PBF_DIR = ROOT / "data" / "geo" / "osm" / "pbf"

# Coastal sailing coverage for REGION_BBOX (lon -25..42, lat 30..72).
_PBF_URLS: dict[str, str] = {
    "britain-and-ireland-latest.osm.pbf": (
        "https://download.geofabrik.de/europe/britain-and-ireland-latest.osm.pbf"
    ),
    "greece-latest.osm.pbf": "https://download.geofabrik.de/europe/greece-latest.osm.pbf",
    "france-latest.osm.pbf": "https://download.geofabrik.de/europe/france-latest.osm.pbf",
    "spain-latest.osm.pbf": "https://download.geofabrik.de/europe/spain-latest.osm.pbf",
    "italy-latest.osm.pbf": "https://download.geofabrik.de/europe/italy-latest.osm.pbf",
    "portugal-latest.osm.pbf": "https://download.geofabrik.de/europe/portugal-latest.osm.pbf",
    "netherlands-latest.osm.pbf": (
        "https://download.geofabrik.de/europe/netherlands-latest.osm.pbf"
    ),
    "belgium-latest.osm.pbf": "https://download.geofabrik.de/europe/belgium-latest.osm.pbf",
    "denmark-latest.osm.pbf": "https://download.geofabrik.de/europe/denmark-latest.osm.pbf",
    "germany-latest.osm.pbf": "https://download.geofabrik.de/europe/germany-latest.osm.pbf",
    "croatia-latest.osm.pbf": "https://download.geofabrik.de/europe/croatia-latest.osm.pbf",
    "norway-latest.osm.pbf": "https://download.geofabrik.de/europe/norway-latest.osm.pbf",
    "sweden-latest.osm.pbf": "https://download.geofabrik.de/europe/sweden-latest.osm.pbf",
    "finland-latest.osm.pbf": "https://download.geofabrik.de/europe/finland-latest.osm.pbf",
    "poland-latest.osm.pbf": "https://download.geofabrik.de/europe/poland-latest.osm.pbf",
    "iceland-latest.osm.pbf": "https://download.geofabrik.de/europe/iceland-latest.osm.pbf",
    "estonia-latest.osm.pbf": "https://download.geofabrik.de/europe/estonia-latest.osm.pbf",
    "latvia-latest.osm.pbf": "https://download.geofabrik.de/europe/latvia-latest.osm.pbf",
    "lithuania-latest.osm.pbf": "https://download.geofabrik.de/europe/lithuania-latest.osm.pbf",
    "malta-latest.osm.pbf": "https://download.geofabrik.de/europe/malta-latest.osm.pbf",
    "cyprus-latest.osm.pbf": "https://download.geofabrik.de/europe/cyprus-latest.osm.pbf",
    "morocco-latest.osm.pbf": "https://download.geofabrik.de/africa/morocco-latest.osm.pbf",
    "algeria-latest.osm.pbf": "https://download.geofabrik.de/africa/algeria-latest.osm.pbf",
    "tunisia-latest.osm.pbf": "https://download.geofabrik.de/africa/tunisia-latest.osm.pbf",
}

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
_MIN_WATER_AREA = 2.0e-7
_LARGE_WATER_AREA = 2.0e-6


def _name(tags: osmium.osm.TagList) -> str:  # type: ignore[name-defined]
    return (tags.get("name:en") or tags.get("name") or "").strip()


def _is_harbour(tags: osmium.osm.TagList) -> bool:  # type: ignore[name-defined]
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


def _notable_island(tags: osmium.osm.TagList) -> bool:  # type: ignore[name-defined]
    return bool(tags.get("wikipedia") or tags.get("wikidata"))


def _harbour_name_ok(name: str) -> bool:
    n = name.lower().strip()
    if not n or "proposed" in n:
        return False
    if n in {"harbour office", "harbor office", "marina office"}:
        return False
    return True


def _water_feature_ok(tags: dict, area: float) -> bool:
    if area < _MIN_WATER_AREA:
        return False
    if tags.get("waterway") == "riverbank":
        return True
    water = (tags.get("water") or "").strip().lower()
    if water in _REJECT_WATER:
        return False
    if water in _KEEP_WATER:
        return True
    if tags.get("natural") == "water":
        return area >= _LARGE_WATER_AREA
    return False


def _tags_dict(tags: osmium.osm.TagList) -> dict[str, str]:  # type: ignore[name-defined]
    return {t.k: t.v for t in tags}


def _ring_coords(ring) -> list[list[float]]:
    coords: list[list[float]] = []
    for node in ring:
        if node.lon == node.lon and node.lat == node.lat:
            coords.append([float(node.lon), float(node.lat)])
    if len(coords) >= 3 and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def _polygon_from_area(a: osmium.osm.Area):  # type: ignore[name-defined]
    """Build shapely polygon(s) from an osmium area (ways + multipolygons)."""
    try:
        outers = []
        for ring in a.outer_rings():
            coords = _ring_coords(ring)
            if len(coords) < 4:
                continue
            holes = []
            for inner in a.inner_rings(ring):
                ic = _ring_coords(inner)
                if len(ic) >= 4:
                    holes.append(ic)
            try:
                poly = make_valid(Polygon(coords, holes))
            except Exception:  # noqa: BLE001
                continue
            if not poly.is_empty and poly.area > 0:
                if poly.geom_type == "Polygon":
                    outers.append(poly)
                elif poly.geom_type == "MultiPolygon":
                    outers.extend(p for p in poly.geoms if p.area > 0)
        if not outers:
            return None
        geom = unary_union(outers) if len(outers) > 1 else outers[0]
        geom = make_valid(geom)
        if geom.is_empty:
            return None
        return geom
    except Exception:  # noqa: BLE001
        return None


class _BandIndex:
    def __init__(self) -> None:
        if not BAND_PATH.exists() or BAND_PATH.stat().st_size < 100_000:
            print("[pbf] building coastal band …", flush=True)
            build_coastal_band(force=True)
        band_geom = load_band_geom()
        if band_geom.geom_type == "MultiPolygon":
            parts = list(band_geom.geoms)
        elif band_geom.geom_type == "Polygon":
            parts = [band_geom]
        else:
            parts = [g for g in getattr(band_geom, "geoms", [band_geom]) if not g.is_empty]
        self.parts = parts
        self.tree = STRtree(parts)
        bb = chart_bbox()
        self.bbox = box(bb["lon_min"], bb["lat_min"], bb["lon_max"], bb["lat_max"])
        print(f"[pbf] coastal band ready ({len(parts)} parts)", flush=True)

    def hits(self, geom) -> bool:
        if geom is None or geom.is_empty:
            return False
        if not self.bbox.intersects(geom):
            return False
        try:
            idxs = self.tree.query(geom)
        except Exception:  # noqa: BLE001
            return False
        for i in idxs:
            try:
                part = self.parts[int(i)]
                if part.intersects(geom):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


class _Writer:
    def __init__(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.places_fp = (WORK / "osm_places.geojsonl").open("w")
        self.roads_fp = (WORK / "osm_roads.geojsonl").open("w")
        self.rivers_fp = (WORK / "osm_rivers.geojsonl").open("w")
        self.water_fp = (WORK / "osm_water.geojsonl").open("w")
        self.place_seen: set[str] = set()
        self.road_seen: set[int] = set()
        self.river_seen: set[int] = set()
        self.water_seen: set[int] = set()
        self.n_places = self.n_roads = self.n_rivers = self.n_water = 0

    def close(self) -> None:
        for fp in (self.places_fp, self.roads_fp, self.rivers_fp, self.water_fp):
            fp.close()

    def write_place(self, feat: dict) -> None:
        props = feat["properties"]
        lon, lat = feat["geometry"]["coordinates"][:2]
        key = f"{props.get('kind')}:{props.get('name')}:{lon:.3f}:{lat:.3f}"
        if key in self.place_seen:
            return
        self.place_seen.add(key)
        self.places_fp.write(json.dumps(feat, separators=(",", ":")) + "\n")
        self.n_places += 1

    def write_road(self, oid: int, feat: dict) -> None:
        if oid in self.road_seen:
            return
        self.road_seen.add(oid)
        self.roads_fp.write(json.dumps(feat, separators=(",", ":")) + "\n")
        self.n_roads += 1

    def write_river(self, oid: int, feat: dict) -> None:
        if oid in self.river_seen:
            return
        self.river_seen.add(oid)
        self.rivers_fp.write(json.dumps(feat, separators=(",", ":")) + "\n")
        self.n_rivers += 1

    def write_water(self, oid: int, feat: dict) -> None:
        if oid in self.water_seen:
            return
        self.water_seen.add(oid)
        self.water_fp.write(json.dumps(feat, separators=(",", ":")) + "\n")
        self.n_water += 1


class _Handler(osmium.SimpleHandler):
    def __init__(self, band: _BandIndex, out: _Writer) -> None:
        super().__init__()
        self.band = band
        self.out = out

    def node(self, n: osmium.osm.Node) -> None:  # type: ignore[name-defined]
        name = _name(n.tags)
        if not name or not n.location.valid():
            return
        lon, lat = float(n.location.lon), float(n.location.lat)
        if not self.band.hits(Point(lon, lat)):
            return

        place = n.tags.get("place")
        if place in _PLACE_META:
            if place in {"island", "archipelago"} and not _notable_island(n.tags):
                pass
            else:
                rank, minzoom, kind = _PLACE_META[place]
                self.out.write_place(
                    {
                        "type": "Feature",
                        "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                        "properties": {
                            "name": name,
                            "kind": kind,
                            "place": place,
                            "rank": rank,
                        },
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    }
                )
                return

        if _is_harbour(n.tags) and _harbour_name_ok(name):
            self.out.write_place(
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 8, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": "harbour",
                        "place": "harbour",
                        "rank": 3,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

    def way(self, w: osmium.osm.Way) -> None:  # type: ignore[name-defined]
        if len(w.nodes) < 2:
            return
        coords: list[list[float]] = []
        for node in w.nodes:
            if node.location.valid():
                coords.append([node.location.lon, node.location.lat])
        if len(coords) < 2:
            return
        try:
            line = LineString(coords)
        except Exception:  # noqa: BLE001
            return
        if not self.band.hits(line):
            return

        name = _name(w.tags)
        place = w.tags.get("place")
        if place in {"island", "archipelago"} and name and _notable_island(w.tags):
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            rank, minzoom, kind = _PLACE_META[place]
            self.out.write_place(
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": kind,
                        "place": place,
                        "rank": rank,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

        if name and _is_harbour(w.tags) and _harbour_name_ok(name):
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            self.out.write_place(
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 8, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": "harbour",
                        "place": "harbour",
                        "rank": 3,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )

        highway = w.tags.get("highway")
        if highway in _ROAD_META:
            minzoom, kind = _ROAD_META[highway]
            self.out.write_road(
                int(w.id),
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                    "properties": {"kind": kind, "highway": highway},
                    "geometry": {"type": "LineString", "coordinates": coords},
                },
            )
            return

        waterway = w.tags.get("waterway")
        if waterway in {"river", "canal"} and name:
            self.out.write_river(
                int(w.id),
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 7, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": "river" if waterway == "river" else "canal",
                    },
                    "geometry": {"type": "LineString", "coordinates": coords},
                },
            )

    def area(self, a: osmium.osm.Area) -> None:  # type: ignore[name-defined]
        tags = a.tags
        name = _name(tags)
        is_water = tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
        place = tags.get("place")
        harbour = _is_harbour(tags)

        if not is_water and not (
            name and (place in {"island", "archipelago"} or harbour)
        ):
            return

        geom = _polygon_from_area(a)
        if geom is None:
            return
        if not self.band.hits(geom):
            return

        oid = int(a.orig_id())
        if is_water:
            tag_dict = _tags_dict(tags)
            if not _water_feature_ok(tag_dict, float(geom.area)):
                return
            oriented = orient(geom, sign=1.0)
            if oriented.geom_type not in {"Polygon", "MultiPolygon"}:
                return
            self.out.write_water(
                oid,
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 6, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": "water",
                        "water": tag_dict.get("water")
                        or tag_dict.get("waterway")
                        or "",
                    },
                    "geometry": mapping(oriented),
                },
            )
            return

        # Centroid label for islands / harbours.
        c = geom.centroid
        lon, lat = float(c.x), float(c.y)
        if place in {"island", "archipelago"} and name and _notable_island(tags):
            rank, minzoom, kind = _PLACE_META[place]
            self.out.write_place(
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": kind,
                        "place": place,
                        "rank": rank,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )
        elif harbour and name and _harbour_name_ok(name):
            self.out.write_place(
                {
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 8, "maxzoom": 14},
                    "properties": {
                        "name": name,
                        "kind": "harbour",
                        "place": "harbour",
                        "rank": 3,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )


def _filter_pbf(src: Path) -> Path:
    """Keep only chart-relevant tags — shrinks multi-GB country extracts a lot."""
    out = WORK / f"filtered_{src.name.replace('.osm.pbf', '')}.osm.pbf"
    if out.exists() and out.stat().st_size > 100_000:
        # Rebuild if source is newer.
        if out.stat().st_mtime >= src.stat().st_mtime:
            print(f"[pbf] reuse filtered {out.name}", flush=True)
            return out
    WORK.mkdir(parents=True, exist_ok=True)
    cmd = [
        "osmium",
        "tags-filter",
        str(src),
        "n/place=city,town,island,archipelago,harbour",
        "n/harbour",
        "n/amenity=marina",
        "n/industrial=harbour",
        "n/seamark:type=harbour,harbour_basin,marina",
        "w/place=island,archipelago",
        "w/harbour",
        "w/amenity=marina",
        "w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link",
        "w/waterway=river,canal,riverbank",
        "wr/natural=water",
        "wr/waterway=riverbank",
        "r/place=island,archipelago",
        "r/harbour",
        "r/amenity=marina",
        "-o",
        str(out),
        "-O",
    ]
    print(f"[pbf] tags-filter {src.name} → {out.name}", flush=True)
    subprocess.run(cmd, check=True)
    print(
        f"[pbf] filtered {out.name} ({out.stat().st_size / 1e6:.1f} MB)",
        flush=True,
    )
    return out


def _download(name: str, url: str) -> Path | None:
    PBF_DIR.mkdir(parents=True, exist_ok=True)
    path = PBF_DIR / name
    if path.exists() and path.stat().st_size > 1_000_000:
        print(
            f"[pbf] already present: {path.name} ({path.stat().st_size / 1e9:.2f} GB)",
            flush=True,
        )
        return path
    part = path.with_suffix(path.suffix + ".part")
    print(f"[pbf] downloading {url}", flush=True)
    try:
        urllib.request.urlretrieve(url, part)
        part.replace(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[pbf] SKIP {name}: {exc}", flush=True)
        if part.exists():
            part.unlink()
        return None
    print(f"[pbf] wrote {path} ({path.stat().st_size / 1e9:.2f} GB)", flush=True)
    return path


def _pbf_names() -> list[str]:
    # Default: every extract we know about. Set PBF_MIN=1 for UK+Greece only.
    if os.environ.get("PBF_MIN") == "1":
        return ["britain-and-ireland-latest.osm.pbf", "greece-latest.osm.pbf"]
    return list(_PBF_URLS.keys())


def main() -> int:
    band = _BandIndex()
    out = _Writer()
    try:
        for name in _pbf_names():
            url = _PBF_URLS[name]
            path = _download(name, url)
            if path is None:
                continue
            try:
                filtered = _filter_pbf(path)
            except Exception as exc:  # noqa: BLE001
                print(f"[pbf] tags-filter failed for {path.name}: {exc}", flush=True)
                filtered = path
            print(f"[pbf] extracting {filtered.name} …", flush=True)
            loc_idx = WORK / f".loc_{filtered.stem}.idx"
            if loc_idx.exists():
                loc_idx.unlink()
            h = _Handler(band, out)
            # Disk-backed node locations — flex_mem OOMs on country extracts.
            h.apply_file(
                str(filtered),
                locations=True,
                idx=f"sparse_file_array,{loc_idx}",
            )
            try:
                loc_idx.unlink()
            except OSError:
                pass
            print(
                f"[pbf]   running totals places={out.n_places} roads={out.n_roads} "
                f"rivers={out.n_rivers} water={out.n_water}",
                flush=True,
            )
    finally:
        out.close()

    print(
        f"[pbf] DONE places={out.n_places} roads={out.n_roads} "
        f"rivers={out.n_rivers} water={out.n_water}",
        flush=True,
    )
    print(f"[pbf] → {WORK / 'osm_places.geojsonl'}", flush=True)
    print(f"[pbf] → {WORK / 'osm_roads.geojsonl'}", flush=True)
    print(f"[pbf] → {WORK / 'osm_rivers.geojsonl'}", flush=True)
    print(f"[pbf] → {WORK / 'osm_water.geojsonl'}", flush=True)
    return 0 if out.n_places > 0 and out.n_water > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
