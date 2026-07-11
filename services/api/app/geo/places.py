"""Natural Earth place / island labels clipped to a bounding box."""

from __future__ import annotations

import json
import logging
from typing import Any

from shapely.geometry import Point, box, shape
from shapely.strtree import STRtree

from ..config import (
    NATURAL_EARTH_GEOGRAPHY,
    NATURAL_EARTH_GEOGRAPHY_POLYS,
    NATURAL_EARTH_PLACES,
)
from ..schemas import BBox

logger = logging.getLogger(__name__)

# Towns: scalerank 0..8 covers regional towns (Portsmouth=7, Southampton=8).
_MAX_TOWN_SCALERANK = 8
_MAX_ISLAND_SCALERANK = 8
# Cap features per bbox so the payload stays light.
_MAX_TOWNS = 80
_MAX_ISLANDS = 60

_ISLAND_FEATURECLAS = frozenset({"island", "island group", "archipelago"})


class _PlaceRecord:
    __slots__ = ("point", "name", "kind", "rank")

    def __init__(self, lon: float, lat: float, name: str, kind: str, rank: float):
        self.point = Point(lon, lat)
        self.name = name
        self.kind = kind  # "town" | "island"
        self.rank = rank  # lower = more important


_town_records: list[_PlaceRecord] | None = None
_island_records: list[_PlaceRecord] | None = None
_town_tree: STRtree | None = None
_island_tree: STRtree | None = None


def _load_geojson(path) -> dict:
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text())


def _clean_island_name(name: str) -> str:
    """Strip redundant 'Island' / 'I.' suffixes for chart labels."""
    n = name.strip()
    for suffix in (" Island", " Is.", " I.", " Islands"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)].strip()
            break
    return n


def _ensure_indexes() -> None:
    global _town_records, _island_records, _town_tree, _island_tree
    if _town_tree is not None and _island_tree is not None:
        return

    towns: list[_PlaceRecord] = []
    places = _load_geojson(NATURAL_EARTH_PLACES)
    for f in places.get("features", []):
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        name = (
            props.get("NAME")
            or props.get("name")
            or props.get("NAMEASCII")
            or props.get("nameascii")
            or ""
        )
        name = str(name).strip()
        if not name:
            continue
        try:
            scalerank = int(props.get("SCALERANK", props.get("scalerank", 10)))
        except (TypeError, ValueError):
            scalerank = 10
        if scalerank > _MAX_TOWN_SCALERANK:
            continue
        try:
            pop = float(props.get("POP_MAX", props.get("pop_max", 0)) or 0)
        except (TypeError, ValueError):
            pop = 0.0
        rank = float(scalerank) - min(pop, 5_000_000) / 5_000_000.0
        towns.append(_PlaceRecord(lon, lat, name, "town", rank))

    islands: list[_PlaceRecord] = []
    seen_names: set[str] = set()

    # Prefer named island / archipelago polygons (good Med + UK coverage).
    polys = _load_geojson(NATURAL_EARTH_GEOGRAPHY_POLYS)
    for f in polys.get("features", []):
        props = f.get("properties") or {}
        featureclas = str(props.get("FEATURECLA") or props.get("featurecla") or "").strip().lower()
        if featureclas not in _ISLAND_FEATURECLAS:
            continue
        try:
            scalerank = int(props.get("SCALERANK", props.get("scalerank", 5)))
        except (TypeError, ValueError):
            scalerank = 5
        if scalerank > _MAX_ISLAND_SCALERANK:
            continue
        name = (
            props.get("NAME_EN")
            or props.get("NAME")
            or props.get("name_en")
            or props.get("name")
            or props.get("LABEL")
            or ""
        )
        name = _clean_island_name(str(name))
        if not name or name.upper() in seen_names:
            continue
        # Skip continental-scale labels that clutter the chart.
        if name.upper() in {"GREAT BRITAIN", "IRELAND", "BRITISH ISLES"}:
            continue
        try:
            geom = shape(f["geometry"])
            if geom.is_empty:
                continue
            c = geom.representative_point()
        except Exception:  # noqa: BLE001
            continue
        seen_names.add(name.upper())
        islands.append(_PlaceRecord(float(c.x), float(c.y), name, "island", float(scalerank)))

    # Supplement with sparse geography points (e.g. Rockall).
    geo = _load_geojson(NATURAL_EARTH_GEOGRAPHY)
    for f in geo.get("features", []):
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        featureclas = str(props.get("featurecla") or props.get("FEATURECLA") or "").strip().lower()
        if featureclas not in _ISLAND_FEATURECLAS:
            continue
        name = _clean_island_name(
            str(
                props.get("name")
                or props.get("NAME")
                or props.get("name_en")
                or props.get("label")
                or ""
            )
        )
        if not name or name.upper() in seen_names:
            continue
        try:
            scalerank = int(props.get("scalerank", props.get("SCALERANK", 5)))
        except (TypeError, ValueError):
            scalerank = 5
        seen_names.add(name.upper())
        islands.append(
            _PlaceRecord(float(coords[0]), float(coords[1]), name, "island", float(scalerank))
        )

    _town_records = towns
    _island_records = islands
    _town_tree = STRtree([r.point for r in towns]) if towns else STRtree([])
    _island_tree = STRtree([r.point for r in islands]) if islands else STRtree([])
    logger.info(
        "Place indexes ready: %d towns, %d islands",
        len(towns),
        len(islands),
    )


def _query_records(
    tree: STRtree,
    records: list[_PlaceRecord],
    clip,
    limit: int,
) -> list[_PlaceRecord]:
    if not records:
        return []
    hits = tree.query(clip)
    selected: list[_PlaceRecord] = []
    for hit in hits:
        idx = int(hit)
        if idx < 0 or idx >= len(records):
            continue
        rec = records[idx]
        if not (clip.covers(rec.point) or clip.intersects(rec.point)):
            continue
        selected.append(rec)
    selected.sort(key=lambda r: r.rank)
    return selected[:limit]


def places_in_bbox(bbox: BBox) -> dict[str, Any]:
    """Return GeoJSON FeatureCollection of town + island labels in bbox."""
    _ensure_indexes()
    assert _town_tree is not None and _island_tree is not None
    assert _town_records is not None and _island_records is not None

    clip = box(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)
    towns = _query_records(_town_tree, _town_records, clip, _MAX_TOWNS)
    islands = _query_records(_island_tree, _island_records, clip, _MAX_ISLANDS)

    features: list[dict[str, Any]] = []
    for rec in islands + towns:
        features.append(
            {
                "type": "Feature",
                "properties": {"name": rec.name, "kind": rec.kind},
                "geometry": {
                    "type": "Point",
                    "coordinates": [rec.point.x, rec.point.y],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
